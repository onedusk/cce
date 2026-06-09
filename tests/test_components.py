"""Tests for the shared component factory (audit-2026-06-09 M05, ADR-001).

Pins the factory contract: parity between embedded and API wiring, the
warn-and-continue fallback for the optional embedding provider, and the
``ComponentSet`` field snapshot — adding a pipeline component means editing
exactly ``cce/components.py``.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from cce.api.app import create_app
from cce.components import ComponentSet, build_components, build_pipeline
from cce.config.loader import load_config
from cce.config.types import (
    CrawlConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    LLMConfig,
)
from cce.discovery.embeddings import EmbeddingUnavailableError
from cce.engine import CurationEngine
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.orchestrator.pipeline import Pipeline

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

    # Reference ComponentSet straight from the factory.
    config = load_config(str(config_yaml))
    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, store)
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
    )

    class _UnreachableProvider:
        def __init__(self, embedding_config: EmbeddingConfig) -> None:
            raise EmbeddingUnavailableError(
                f"Ollama server unreachable at {embedding_config.base_url}"
            )

    monkeypatch.setattr(
        "cce.discovery.ollama.OllamaEmbeddingProvider", _UnreachableProvider
    )

    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        with caplog.at_level(logging.WARNING, logger="cce.components"):
            components = build_components(config, store)
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

    store = SQLiteEvidenceStore(config.evidence_store)
    await store.connect()
    try:
        components = build_components(config, store)
        pipeline = build_pipeline(config, store, components)
    finally:
        await store.close()

    assert components.scorer is not None
    assert pipeline._scorer is components.scorer
    assert pipeline._taxonomy_plugin is components.taxonomy
    assert pipeline._discoverer._embedding is components.embedding
    assert pipeline._path_configs == components.path_configs


def test_componentset_completeness_snapshot():
    """Field snapshot: a new ComponentSet field has no default, so the
    factory's constructor call fails until build_components is updated —
    adding a component means editing exactly cce/components.py. This
    snapshot makes that contract explicit and reviewed."""
    assert [f.name for f in dataclasses.fields(ComponentSet)] == [
        "llm",
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
