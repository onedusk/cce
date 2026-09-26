"""Two Pipelines with two stores in one process never see each other's rows (B6).

Both Pipelines share ONE ComponentSet (the supported multi-tenant pattern:
components hold no tenant data). Before B6 the implied-claim checker was
bound to the store the ComponentSet was built with, so tenant B's
counter-evidence search read tenant A's excerpts. A token planted only in
tenant A must reach nothing of tenant B's: prompts, package or store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cce.components import ComponentOverrides, build_components, build_pipeline
from cce.config.registry import ConfigRegistry
from cce.config.types import (
    CrawlConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    LLMConfig,
    QualityGateConfig,
)
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.base import LLMResponse
from cce.models.evidence import Evidence
from cce.models.job import JobStage
from cce.synthesis.implied_claims import _DISMISSED_TOPIC_PROMPT
from tests.conftest import (
    MockCrawlAdapter,
    MockLLMProvider,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)
from tests.test_orchestrator.conftest import verifier_json
from tests.test_orchestrator.test_pipeline_implied_claims import (
    _ai_flat_with_contrast,
    _editor_response,
    _topic_extract_response,
)

pytestmark = pytest.mark.integration

SHARED_URL = "https://shared.example.com/sleep"
PLANTED_ID = "ev_a1a1a1a1a1a1"


class _RecordingAdapter(MockCrawlAdapter):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.crawled: list[str] = []

    async def crawl(self, request):  # type: ignore[no-untyped-def]
        self.crawled.append(request.url)
        return await super().crawl(request)


class _SearchSpy:
    """Store wrapper recording counter-evidence searches."""

    def __init__(self, real) -> None:
        self._real = real
        self.search_calls: list[str | None] = []

    async def search(self, *, url=None, topic=None, limit=50):
        self.search_calls.append(topic)
        return await self._real.search(url=url, topic=topic, limit=limit)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _one_run_script() -> list[LLMResponse]:
    """Writer (flagged draft with 'Unlike sleeping pills') -> topic extraction
    -> editor -> verifier."""
    return [
        LLMResponse(content=_ai_flat_with_contrast(), stop_reason="end_turn"),
        LLMResponse(
            content=_topic_extract_response("sleeping pills"), stop_reason="end_turn"
        ),
        LLMResponse(
            content=_editor_response("Sleep matters a great deal [ev:ev_001]."),
            stop_reason="end_turn",
        ),
        LLMResponse(
            content=verifier_json(supported=10, total=10, gaps=0),
            stop_reason="end_turn",
        ),
    ]


def _calls_text(calls: list[dict]) -> str:
    return "\n".join(
        (c["system"] or "") + "\n" + "\n".join(m.content for m in c["messages"])
        for c in calls
    )


async def test_tenants_sharing_components_never_see_each_others_rows(tmp_path: Path):
    token = "TENANTA-" + uuid.uuid4().hex
    planted = Evidence(
        id=PLANTED_ID,
        url=SHARED_URL,
        excerpt=f"{token} sleeping pills help short-term insomnia in controlled trials.",
        excerpt_hash=uuid.uuid4().hex,
        retrieved_at=datetime(2024, 3, 1, tzinfo=UTC),
        locator="chunk:0",
    )
    store_a = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / "a.db"))
    store_b = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / "b.db"))
    await store_a.connect()
    await store_b.connect()
    await store_a.put(planted)
    spy_a = _SearchSpy(store_a)

    config = EngineConfig(
        llm=LLMConfig(api_key=""),
        crawl=CrawlConfig(api_key=None),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=True),
        quality_gate={"medium": QualityGateConfig(max_writer_iterations=1)},
    )
    registry = ConfigRegistry.load(
        Path("."),
        engine=config,
        taxonomies_dir=tmp_path / "no-taxonomies",
        path_configs_path=tmp_path / "no-path-configs.yaml",
    )
    adapter = _RecordingAdapter(
        search_map={"test topic": [SHARED_URL]},
        url_map={
            SHARED_URL: make_crawl_result(
                url=SHARED_URL,
                markdown=(
                    "# Sleep\n\nA regular schedule and a dark, cool room are the "
                    "habits the sleep literature recommends most consistently."
                ),
            )
        },
    )
    fake_llm = MockLLMProvider(
        _one_run_script() + _one_run_script(), cite_placeholders=True
    )
    components = build_components(
        config,
        registry,
        overrides=ComponentOverrides(llm=fake_llm, crawl_adapter=adapter),
    )
    pipe_a = build_pipeline(config, registry, spy_a, components)
    pipe_b = build_pipeline(config, registry, store_b, components)

    try:
        # --- Tenant B runs first ---
        result_b = await pipe_b.run(make_curation_request(), make_source_policy())
        b_calls = list(fake_llm.calls)

        # Discovery did not reuse A's row for the shared URL.
        assert SHARED_URL in adapter.crawled
        assert result_b.package is not None
        assert all(token not in ev.excerpt for ev in result_b.package.evidence)
        assert all(ev.id != PLANTED_ID for ev in result_b.package.evidence)
        # Nothing of A's reached any of B's prompts (writer/verifier evidence
        # blocks, and the editor prompt that carries the checker's hint IDs).
        b_text = _calls_text(b_calls)
        assert token not in b_text and PLANTED_ID not in b_text
        # The implied-claim checker really ran for B — on B's store, not A's.
        assert any((c["system"] or "") == _DISMISSED_TOPIC_PROMPT for c in b_calls)
        assert any(r.stage == JobStage.EDIT for r in result_b.job.stages)
        assert spy_a.search_calls == []
        assert await store_b.search(topic=token) == []
        assert await store_a.count() == 1

        # --- Positive control: tenant A, same components ---
        adapter.crawled.clear()
        result_a = await pipe_a.run(make_curation_request(), make_source_policy())
        a_calls = fake_llm.calls[len(b_calls) :]

        # In-tenant URL reuse: A's stored row served the shared URL...
        assert SHARED_URL not in adapter.crawled
        assert any(ev.id == PLANTED_ID for ev in result_a.package.evidence)
        # ...and A's checker searched A's store and found the planted row.
        assert spy_a.search_calls == ["sleeping pills"]
        editor_calls = [
            c
            for c in a_calls
            if "Implied-claim annotations" in c["messages"][0].content
        ]
        assert editor_calls and PLANTED_ID in editor_calls[0]["messages"][0].content
    finally:
        await store_a.close()
        await store_b.close()
