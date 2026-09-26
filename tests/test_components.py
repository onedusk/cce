"""Tests for the shared component factory (audit-2026-06-09 M05, ADR-001).

Pins the factory contract: parity between embedded and API wiring, the
warn-and-continue fallback for the optional embedding provider, and the
``ComponentSet`` field snapshot — adding a pipeline component means editing
exactly ``cce/components.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import re
from pathlib import Path

import pytest

from cce.api.app import create_app
from cce.components import (
    ComponentOverrides,
    ComponentSet,
    build_components,
    build_pipeline,
)
from cce.config.loader import load_config
from cce.config.registry import ConfigRegistry
from cce.config.types import (
    CrawlConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    LLMConfig,
    QualityGateConfig,
    VerifierConfig,
)
from cce.discovery.embeddings import EmbeddingUnavailableError
from cce.engine import CurationEngine
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.base import LLMResponse
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from cce.synthesis.editor import EDITOR_SYSTEM_PROMPT
from cce.synthesis.implied_claims import _DISMISSED_TOPIC_PROMPT
from cce.synthesis.writer import WRITER_SYSTEM_PROMPT
from cce.verification.verifier import _VERIFIER_FULL_PROMPT
from tests.conftest import (
    MockCrawlAdapter,
    MockEmbeddingProvider,
    MockLLMProvider,
    make_curation_request,
    make_source_policy,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config_yaml(
    tmp_path: Path, *, embedding_enabled: bool, humanization_enabled: bool
) -> Path:
    """One config file drives both modes — the parity precondition."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "evidence_store:\n"
        f"  sqlite_path: {tmp_path / 'components_test.db'}\n"
        "embedding:\n"
        f"  enabled: {str(embedding_enabled).lower()}\n"
        "humanization:\n"
        f"  enabled: {str(humanization_enabled).lower()}\n"
        "  editor:\n"
        f"    enabled: {str(humanization_enabled).lower()}\n"
        "  implied_claims:\n"
        f"    enabled: {str(humanization_enabled).lower()}\n"
        "api:\n"
        "  require_auth: false\n"
        "  max_concurrent_jobs: 2\n"
    )
    return config_yaml


def _pipeline_component_types(pipeline: Pipeline) -> dict[str, type]:
    """Optional-component types present on a built Pipeline (None-safe)."""
    return {
        "embedding": type(pipeline._discoverer._embedding),
        "taxonomy": type(pipeline._taxonomy_plugin),
        "scorer": type(pipeline._scorer),
        "editor": type(pipeline._editor),
        "implied_claims": type(pipeline._implied_claim_checker),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("humanization_enabled", [True, False])
@pytest.mark.parametrize("embedding_enabled", [True, False])
async def test_parity_embedded_vs_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedding_enabled: bool,
    humanization_enabled: bool,
):
    """Same config -> identical component types in embedded and API modes.

    Both modes route through cce.components, so flipping either config
    toggle must change both identically. Asserts by type, not identity.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    monkeypatch.delenv("CCE_EVIDENCE_SQLITE_PATH", raising=False)
    config_yaml = _write_config_yaml(
        tmp_path,
        embedding_enabled=embedding_enabled,
        humanization_enabled=humanization_enabled,
    )

    # Reference ComponentSet straight from the factory. Registry from the
    # repo-root cwd — the same tree both wiring modes load from.
    config = load_config(str(config_yaml))
    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, registry)
    finally:
        await store.close()

    assert (components.embedding is not None) == embedding_enabled
    assert (components.scorer is not None) == humanization_enabled
    assert (components.editor is not None) == humanization_enabled
    assert (components.implied_claims is not None) == humanization_enabled

    # Embedded mode — the real factory line.
    engine = await CurationEngine.embedded(
        config_path=str(config_yaml),
        policies_dir=str(tmp_path / "no-policies"),
    )
    try:
        assert engine._pipeline is not None
        embedded_types = _pipeline_component_types(engine._pipeline)
    finally:
        await engine.close()

    # API mode — the real lifespan production branch.
    app = create_app(load_config(str(config_yaml)))
    async with app.router.lifespan_context(app):
        api_types = _pipeline_component_types(app.state.pipeline)

    assert embedded_types == api_types
    assert embedded_types == {
        "embedding": type(components.embedding),
        "taxonomy": type(components.taxonomy),
        "scorer": type(components.scorer),
        "editor": type(components.editor),
        "implied_claims": type(components.implied_claims),
    }


async def test_embedding_fallback_warn_and_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """embedding.enabled + unreachable server -> embedding=None + warning.

    OllamaEmbeddingProvider does not probe the network at construction time,
    so an unreachable base URL alone cannot fail the constructor; substitute
    a constructor that raises EmbeddingUnavailableError the way a probing
    provider would, and pin the warn-and-continue branch lifted from the old
    wiring sites.
    """
    # No taxonomies/ or path_configs/ here — also pins the load-or-None paths.
    monkeypatch.chdir(tmp_path)
    config = EngineConfig(
        llm=LLMConfig(api_key="test-key"),
        crawl=CrawlConfig(api_key="test-key"),
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"),
        embedding=EmbeddingConfig(enabled=True, base_url="http://127.0.0.1:9"),
        humanization=HumanizationConfig(enabled=False),  # isolate the embedding path
    )

    class _UnreachableProvider:
        def __init__(self, embedding_config: EmbeddingConfig) -> None:
            raise EmbeddingUnavailableError(
                f"Ollama server unreachable at {embedding_config.base_url}"
            )

    monkeypatch.setattr(
        "cce.discovery.ollama.OllamaEmbeddingProvider", _UnreachableProvider
    )

    # cwd (tmp_path) has no optional config trees -> empty registry surfaces.
    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        with caplog.at_level(logging.WARNING, logger="cce.components"):
            components = build_components(config, registry)
    finally:
        await store.close()

    assert components.embedding is None
    assert "Embedding provider unavailable" in caplog.text
    # cwd has no optional config files -> graceful absence, not an error.
    assert components.taxonomy is None
    assert components.path_configs == {}


async def test_build_pipeline_accepts_prebuilt_components(tmp_path: Path):
    """build_pipeline(components=...) wires the given set without rebuilding."""
    config = EngineConfig(
        llm=LLMConfig(api_key="test-key"),
        crawl=CrawlConfig(api_key="test-key"),
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=True),
    )

    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, registry)
        pipeline = build_pipeline(config, registry, store, components)
    finally:
        await store.close()

    assert components.scorer is not None
    assert pipeline._scorer is components.scorer
    assert pipeline._taxonomy_plugin is components.taxonomy
    assert pipeline._discoverer._embedding is components.embedding
    assert pipeline._path_configs == components.path_configs


def _b3_config(tmp_path: Path, verifier_model: str | None) -> EngineConfig:
    return EngineConfig(
        llm=LLMConfig(api_key="test-key", model="claude-sonnet-4-6"),
        verifier=VerifierConfig(model=verifier_model),
        crawl=CrawlConfig(api_key="test-key"),
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=True),
    )


async def test_verifier_model_gets_its_own_provider(tmp_path: Path):
    """B3: verifier.model builds a second provider with inherited
    credentials; only the Verifier moves — writer, editor and implied-claim
    checker keep the main provider."""
    config = _b3_config(tmp_path, verifier_model="claude-opus-5")
    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, registry)
        pipeline = build_pipeline(config, registry, store, components)
    finally:
        await store.close()

    assert components.verifier_llm is not components.llm
    assert components.verifier_llm._config.model == "claude-opus-5"
    assert components.verifier_llm._config.api_key == "test-key"
    assert components.llm._config.model == "claude-sonnet-4-6"
    assert pipeline._verifier._llm is components.verifier_llm
    assert pipeline._writer._llm is components.llm
    assert components.editor is not None
    assert components.editor._llm is components.llm
    assert components.implied_claims is not None
    assert components.implied_claims._llm is components.llm


async def test_verifier_model_unset_shares_the_main_provider(tmp_path: Path):
    """B3: unset, behaviour is unchanged — one provider for every role."""
    config = _b3_config(tmp_path, verifier_model=None)
    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, registry)
        pipeline = build_pipeline(config, registry, store, components)
    finally:
        await store.close()

    assert components.verifier_llm is components.llm
    assert pipeline._verifier._llm is components.llm


async def test_verifier_requests_use_the_verifier_model(tmp_path: Path):
    """B3 acceptance: with verifier.model set, the Verifier's SDK requests
    carry its own model ID while the Writer's carry the main one."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from tests.conftest import make_content_unit, make_curation_request, make_evidence

    def _sdk_response(text: str) -> MagicMock:
        block = MagicMock()
        block.text = text
        response = MagicMock()
        response.content = [block]
        response.model = "echo"
        response.usage = MagicMock(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        response.stop_reason = "end_turn"
        return response

    config = _b3_config(tmp_path, verifier_model="claude-opus-5")
    registry = ConfigRegistry.load(Path("."), engine=config)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        with patch("cce.llm.anthropic.anthropic.AsyncAnthropic") as mock_cls:
            client = MagicMock()
            # One reply that satisfies both the writer and verifier shapes.
            counts = dict.fromkeys(
                (
                    "total_claims",
                    "supported",
                    "unsupported",
                    "uncited",
                    "leakage",
                    "conflicts",
                    "gaps_acknowledged",
                ),
                0,
            )
            reply = json.dumps({"content": "x", "claims": [], "summary": counts})
            client.messages.create = AsyncMock(return_value=_sdk_response(reply))
            mock_cls.return_value = client
            pipeline = build_pipeline(config, registry, store)

            evidence = [make_evidence(id="ev_001")]
            await pipeline._writer.write(make_curation_request(), evidence, "learn")
            await pipeline._verifier.verify(
                make_content_unit(content="Claim [ev:ev_001]."), evidence
            )
    finally:
        await store.close()

    models = [c.kwargs["model"] for c in client.messages.create.call_args_list]
    assert models == ["claude-sonnet-4-6", "claude-opus-5"]


def test_componentset_completeness_snapshot():
    """Field snapshot: a new ComponentSet field has no default, so the
    factory's constructor call fails until build_components is updated —
    adding a component means editing exactly cce/components.py. This
    snapshot makes that contract explicit and reviewed."""
    assert [f.name for f in dataclasses.fields(ComponentSet)] == [
        "llm",
        "verifier_llm",
        "crawl_adapter",
        "embedding",
        "taxonomy",
        "path_configs",
        "scorer",
        "editor",
        "implied_claims",
    ]
    assert all(
        f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        for f in dataclasses.fields(ComponentSet)
    )


def test_no_direct_loader_calls_in_wiring_sources():
    """Drift tripwire (finding 1.3, M06): engine.py and api/app.py must not
    call the YAML loaders directly — all loading goes through ConfigRegistry.
    Source inspection rather than a one-off grep so CI catches regressions."""
    import cce.api.app
    import cce.engine

    forbidden = re.compile(
        r"load_policies\(|load_path_configs\(|load_taxonomy\(|load_markers\("
    )
    for module in (cce.engine, cce.api.app):
        source = inspect.getsource(module)
        match = forbidden.search(source)
        assert match is None, (
            f"{module.__name__} calls {match.group(0)!r} directly; "
            "route configuration loading through ConfigRegistry (ADR-002)"
        )


async def test_build_components_rejects_registry_without_markers(tmp_path: Path):
    """Humanization enabled + registry built without markers is a wiring bug
    (registry from a different config) — fail loudly, never score-silently."""
    config = EngineConfig(
        llm=LLMConfig(api_key="test-key"),
        crawl=CrawlConfig(api_key="test-key"),
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=True),
    )
    registry = ConfigRegistry(
        engine=EngineConfig(llm=LLMConfig(api_key="test-key"))
    )  # humanization off -> no markers

    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        with pytest.raises(ValueError, match="holds no markers"):
            build_components(config, registry)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Provider injection (B5)
# ---------------------------------------------------------------------------


class _RecordingAdapter(MockCrawlAdapter):
    """MockCrawlAdapter that records search and crawl calls."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.searches: list[str] = []
        self.crawled: list[str] = []

    async def search(self, query: str, limit: int = 10) -> list[str]:
        self.searches.append(query)
        return await super().search(query, limit)

    async def crawl(self, request):  # type: ignore[no-untyped-def]
        self.crawled.append(request.url)
        return await super().crawl(request)


def _b5_config(tmp_path: Path, **overrides) -> EngineConfig:
    return EngineConfig(
        llm=LLMConfig(api_key=""),
        crawl=CrawlConfig(api_key=None),
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=True),
        quality_gate={"medium": QualityGateConfig(max_writer_iterations=1)},
        **overrides,
    )


def _b5_registry(config: EngineConfig, tmp_path: Path) -> ConfigRegistry:
    # Explicit empty taxonomy / path-config surfaces: the repo root may hold
    # gitignored operator files that would change behaviour locally vs CI.
    return ConfigRegistry.load(
        Path("."),
        engine=config,
        taxonomies_dir=tmp_path / "no-taxonomies",
        path_configs_path=tmp_path / "no-path-configs.yaml",
    )


class _Unbuildable:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("a concrete provider was built despite the override")


async def test_overrides_reach_every_llm_and_crawl_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """B5 acceptance: injected fakes are used by discovery, the Writer, the
    Verifier, the Editor and the implied-claim checker — and no concrete
    Anthropic/Firecrawl client is constructed."""
    from tests.test_orchestrator.conftest import make_adapter, verifier_json
    from tests.test_orchestrator.test_pipeline_implied_claims import (
        _ai_flat_with_contrast,
        _editor_response,
        _topic_extract_response,
    )

    monkeypatch.setattr("cce.llm.anthropic.AnthropicProvider", _Unbuildable)
    monkeypatch.setattr(
        "cce.discovery.adapters.firecrawl.FirecrawlAdapter", _Unbuildable
    )

    template = make_adapter()
    adapter = _RecordingAdapter(
        url_map=template._url_map, search_map=template._search_map
    )
    fake_llm = MockLLMProvider(
        [
            LLMResponse(content=_ai_flat_with_contrast(), stop_reason="end_turn"),
            LLMResponse(
                content=_topic_extract_response("sleeping pills"),
                stop_reason="end_turn",
            ),
            LLMResponse(
                content=_editor_response("Sleep matters a great deal [ev:ev_001]."),
                stop_reason="end_turn",
            ),
            LLMResponse(
                content=verifier_json(supported=10, total=10, gaps=0),
                stop_reason="end_turn",
            ),
        ],
        cite_placeholders=True,
    )

    config = _b5_config(tmp_path)
    registry = _b5_registry(config, tmp_path)
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        pipeline = build_pipeline(
            config,
            registry,
            store,
            overrides=ComponentOverrides(llm=fake_llm, crawl_adapter=adapter),
        )
        result = await pipeline.run(make_curation_request(), make_source_policy())
    finally:
        await store.close()

    # Identity: every consumer holds the injected objects.
    assert pipeline._discoverer._adapter is adapter
    assert pipeline._writer._llm is fake_llm
    assert pipeline._verifier._llm is fake_llm
    assert pipeline._editor is not None and pipeline._editor._llm is fake_llm
    checker = pipeline._implied_claim_checker
    assert checker is not None and checker._llm is fake_llm

    # Behaviour: each role actually called through the fakes.
    assert adapter.searches and adapter.crawled
    systems = [c["system"] or "" for c in fake_llm.calls]
    for prompt in (
        WRITER_SYSTEM_PROMPT,
        _DISMISSED_TOPIC_PROMPT,
        EDITOR_SYSTEM_PROMPT,
        _VERIFIER_FULL_PROMPT,
    ):
        assert any(s.startswith(prompt) for s in systems), prompt[:40]
    stages = {rec.stage for rec in result.job.stages}
    assert {
        JobStage.DISCOVER,
        JobStage.WRITE,
        JobStage.SCORE,
        JobStage.EDIT,
        JobStage.VERIFY,
    } <= stages


async def test_verifier_llm_override_routes_only_the_verifier(tmp_path: Path):
    writer_llm, verifier_llm = MockLLMProvider(), MockLLMProvider()
    config = _b5_config(tmp_path)
    components = build_components(
        config,
        _b5_registry(config, tmp_path),
        overrides=ComponentOverrides(
            llm=writer_llm, verifier_llm=verifier_llm, crawl_adapter=MockCrawlAdapter()
        ),
    )

    assert components.llm is writer_llm
    assert components.verifier_llm is verifier_llm
    assert components.editor is not None and components.editor._llm is writer_llm


async def test_llm_override_shares_verifier_when_no_verifier_model(tmp_path: Path):
    llm = MockLLMProvider()
    config = _b5_config(tmp_path)
    components = build_components(
        config,
        _b5_registry(config, tmp_path),
        overrides=ComponentOverrides(llm=llm, crawl_adapter=MockCrawlAdapter()),
    )

    assert components.verifier_llm is llm


async def test_llm_override_with_verifier_model_raises(tmp_path: Path):
    """Building the verifier from config would bypass the injected gateway."""
    config = _b5_config(tmp_path, verifier=VerifierConfig(model="claude-opus-5"))
    with pytest.raises(ValueError, match="verifier_llm"):
        build_components(
            config,
            _b5_registry(config, tmp_path),
            overrides=ComponentOverrides(
                llm=MockLLMProvider(), crawl_adapter=MockCrawlAdapter()
            ),
        )


async def test_embedding_override_is_used_even_when_disabled_in_config(tmp_path: Path):
    embedding = MockEmbeddingProvider()
    config = _b5_config(tmp_path)
    components = build_components(
        config,
        _b5_registry(config, tmp_path),
        overrides=ComponentOverrides(
            llm=MockLLMProvider(),
            crawl_adapter=MockCrawlAdapter(),
            embedding=embedding,
        ),
    )

    assert components.embedding is embedding


async def test_build_pipeline_rejects_components_plus_overrides(tmp_path: Path):
    config = _b5_config(tmp_path)
    registry = _b5_registry(config, tmp_path)
    overrides = ComponentOverrides(
        llm=MockLLMProvider(), crawl_adapter=MockCrawlAdapter()
    )
    components = build_components(config, registry, overrides=overrides)
    with pytest.raises(ValueError, match="not both"):
        build_pipeline(config, registry, None, components, overrides=overrides)  # type: ignore[arg-type]
