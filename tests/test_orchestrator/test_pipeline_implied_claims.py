"""Integration tests for the implied-claim checker inside the Pipeline (M04).

The checker must:
- Run before the editor when scoring fails.
- Be skipped when the scorer passes.
- Be skipped when no editor is wired (no consumer for annotations).
- Honor the release valve when counter-evidence is small relative to cited.
- Not run when the writer's draft contains no contrastive frames.
"""

from __future__ import annotations

import json

import pytest

from cce.config.markers import load_markers
from cce.config.types import (
    CrawlConfig,
    EditorConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    HumanizationThresholds,
    ImpliedClaimsConfig,
    LLMConfig,
)
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.base import LLMResponse
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from cce.synthesis.editor import Editor
from cce.synthesis.implied_claims import ImpliedClaimChecker
from cce.synthesis.scoring import Scorer
from tests.conftest import (
    MockLLMProvider,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    make_adapter as _make_adapter,
)
from tests.test_orchestrator.conftest import (
    verifier_json as _verifier_json,
)

pytestmark = pytest.mark.integration


def _markers():
    return load_markers("config/humanization_markers.yaml")


def _scorer() -> Scorer:
    return Scorer(thresholds=HumanizationThresholds(), markers=_markers())


def _editor(llm: MockLLMProvider) -> Editor:
    return Editor(llm=llm, config=EditorConfig(enabled=True))


def _checker(
    llm: MockLLMProvider,
    store,
    config: ImpliedClaimsConfig | None = None,
) -> ImpliedClaimChecker:
    return ImpliedClaimChecker(
        llm=llm,
        evidence_store=store,
        config=config or ImpliedClaimsConfig(enabled=True),
        markers=_markers(),
    )


def _ai_flat_with_contrast() -> str:
    """Writer output flagged by the scorer AND containing a contrastive frame."""
    body = (
        "Furthermore, sleep is crucial [ev:ev_001]. "
        "Unlike sleeping pills, the comprehensive landscape of sleep is "
        "multifaceted [ev:ev_001]. "
        "Additionally, the pivotal role of sleep is showcased [ev:ev_001]. "
        "Moreover, sleep underscores robust effects [ev:ev_001]."
    )
    return json.dumps(
        {
            "content": body,
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "Sleep matters", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


def _ai_flat_no_contrast() -> str:
    """Writer output flagged by the scorer with NO contrastive frame."""
    body = (
        "Furthermore, sleep is crucial [ev:ev_001]. "
        "Additionally, the comprehensive landscape of sleep is "
        "multifaceted [ev:ev_001]. "
        "Moreover, the pivotal role of sleep is showcased [ev:ev_001]. "
        "In conclusion, sleep underscores robust effects [ev:ev_001]."
    )
    return json.dumps(
        {
            "content": body,
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "Sleep matters", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


def _editor_response(content: str) -> str:
    return json.dumps({"edited_content": content, "notes": "rewritten with spectrum"})


def _topic_extract_response(topic: str) -> str:
    return json.dumps({"dismissed_topic": topic, "rationale": "extracted from frame"})


def _llm(*scripted: str) -> MockLLMProvider:
    return MockLLMProvider(
        [LLMResponse(content=s, model="mock", stop_reason="end_turn") for s in scripted]
    )


# ---------------------------------------------------------------------------


async def test_checker_invoked_before_editor_when_score_fails(sqlite_store):
    """Contrastive frame in writer output → checker fires → editor receives
    the rewrite hint via annotations."""
    config = make_engine_config()
    adapter = _make_adapter()
    rewritten = (
        "Sleep matters [ev:ev_001]. Sleeping pills can help in acute insomnia, "
        "but they don't change the habits that keep the problem coming back."
    )
    # LLM call sequence: writer → topic-extractor → editor → verifier
    llm = _llm(
        _ai_flat_with_contrast(),
        _topic_extract_response("sleeping pills"),
        _editor_response(rewritten),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    # Stub a store that returns enough counter-evidence to clear the release valve
    class CounterStore:
        def __init__(self, real):
            self._real = real
            self.search_calls = []

        async def search(self, *, url=None, topic=None, limit=50):
            self.search_calls.append({"topic": topic, "limit": limit})
            # Return 5 evidence rows so ratio comfortably exceeds 0.15
            return [
                # Reuse make_evidence pattern via a quick dict-to-Evidence
                _make_counter_ev(f"ev_counter_{i}")
                for i in range(5)
            ]

        # Forward all other store methods to the real one
        def __getattr__(self, name):
            return getattr(self._real, name)

    counter_store = CounterStore(sqlite_store)

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
        implied_claim_checker=_checker(llm, counter_store),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    assert len(edit_records) == 1
    # Editor's user-prompt was the third LLM call (writer=0, topic=1, editor=2, verifier=3)
    editor_call = llm.calls[2]
    editor_user_msg = editor_call["messages"][0].content
    assert "Implied-claim annotations" in editor_user_msg
    assert "sleeping pills" in editor_user_msg


async def test_checker_skipped_when_score_passes(sqlite_store):
    """Scorer passes → editor doesn't fire → checker doesn't fire either.
    Verified by absence of any topic-extraction LLM calls."""
    config = make_engine_config()
    adapter = _make_adapter()
    # Bursty writer fixture (passes the scorer), then standard verifier
    bursty = json.dumps(
        {
            "content": (
                "Sleep breaks down into stages [ev:ev_001]. Each one does "
                "different work — some repair the body, some consolidate "
                "memory, some don't seem to do much we can name. Wake "
                "someone mid-REM and they'll report vivid dreams.\n\n"
                "CBT-I targets the habits keeping people awake [ev:ev_001]. "
                "It works. Six to eight sessions, with homework. The pattern "
                "shows up in the research: people who finish sleep better a "
                "year later, two years later, five years later, which is "
                "more than you can say for most sleep medications."
            ),
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "stages", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )
    llm = _llm(
        bursty,
        _verifier_json(supported=10, total=10, gaps=0),
        bursty,
        _verifier_json(supported=10, total=10, gaps=0),
        bursty,
        _verifier_json(supported=10, total=10, gaps=0),
    )

    class TrackingStore:
        def __init__(self, real):
            self._real = real
            self.search_calls = []

        async def search(self, *, url=None, topic=None, limit=50):
            self.search_calls.append({"topic": topic, "limit": limit})
            return []

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = TrackingStore(sqlite_store)
    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
        implied_claim_checker=_checker(llm, store),
    )
    await pipeline.run(make_curation_request(), make_source_policy())

    # Checker would call store.search; that should never have happened.
    assert store.search_calls == []


async def test_checker_skipped_when_no_editor_wired(sqlite_store):
    """No editor → no consumer for annotations → checker should not fire."""
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(
        _ai_flat_with_contrast(),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    class TrackingStore:
        def __init__(self, real):
            self._real = real
            self.search_calls = []

        async def search(self, *, url=None, topic=None, limit=50):
            self.search_calls.append({"topic": topic, "limit": limit})
            return []

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = TrackingStore(sqlite_store)
    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=None,  # explicit
        implied_claim_checker=_checker(llm, store),
    )
    await pipeline.run(make_curation_request(), make_source_policy())

    # Checker is gated on editor being present; store.search must never have run.
    assert store.search_calls == []


async def test_checker_emits_no_annotations_when_no_contrastive_frames(sqlite_store):
    """AI-flat draft without any contrastive constructions → no annotations,
    editor still fires (scorer flagged the draft) but with annotations=None."""
    config = make_engine_config()
    adapter = _make_adapter()
    rewritten = "Sleep matters [ev:ev_001]. Studies converge on this consistently."
    llm = _llm(
        _ai_flat_no_contrast(),  # no contrastive frame
        _editor_response(rewritten),  # editor still fires (scorer failed)
        _verifier_json(supported=10, total=10, gaps=0),
    )

    class CounterStore:
        def __init__(self, real):
            self._real = real
            self.search_calls = []

        async def search(self, *, url=None, topic=None, limit=50):
            self.search_calls.append({"topic": topic, "limit": limit})
            return []

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = CounterStore(sqlite_store)
    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
        implied_claim_checker=_checker(llm, store),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    assert len(edit_records) == 1
    # No frames → no LLM calls into the topic extractor → no store search
    assert store.search_calls == []
    # Editor's user-prompt has NO implied-claim annotation block
    editor_call = llm.calls[1]  # writer=0, editor=1, verifier=2
    assert "Implied-claim annotations" not in editor_call["messages"][0].content


async def test_checker_factory_triple_gate(monkeypatch, tmp_path):
    """_build_pipeline only constructs the checker when humanization.enabled
    AND humanization.implied_claims.enabled (and scorer/editor follow their
    own gates independently)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-dummy")

    from cce.api.app import _build_pipeline

    store = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"))
    await store.connect()
    crawl = CrawlConfig(api_key="test-dummy")

    cfg_master_off = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(enabled=False),
    )
    pipe_off = _build_pipeline(cfg_master_off, store)
    assert pipe_off._implied_claim_checker is None

    cfg_master_only = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(enabled=True),
    )
    pipe_master_only = _build_pipeline(cfg_master_only, store)
    assert pipe_master_only._scorer is not None
    assert pipe_master_only._editor is None
    assert pipe_master_only._implied_claim_checker is None

    cfg_all = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(
            enabled=True,
            editor=EditorConfig(enabled=True),
            implied_claims=ImpliedClaimsConfig(enabled=True),
        ),
    )
    pipe_all = _build_pipeline(cfg_all, store)
    assert pipe_all._scorer is not None
    assert pipe_all._editor is not None
    assert pipe_all._implied_claim_checker is not None


# --- helpers ---


def _make_counter_ev(ev_id: str):
    """Build a minimal Evidence for the counter-store stub.

    Avoids the ``make_evidence`` factory's UUID-based id so we can assert on
    a stable id in the editor's prompt.
    """
    import hashlib
    from datetime import UTC, datetime

    from cce.models.evidence import Evidence, SourceQuality

    excerpt = f"Counter-evidence for {ev_id}: longer text to clear length checks."
    return Evidence(
        id=ev_id,
        url=f"https://example.com/{ev_id}",
        title=f"Counter source {ev_id}",
        author="Counter Author",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        retrieved_at=datetime(2024, 3, 1, tzinfo=UTC),
        excerpt=excerpt,
        excerpt_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
        locator="chunk:0",
        source_quality=SourceQuality(
            is_peer_reviewed=False,
            is_primary_source=False,
            domain_reputation="unknown",
            conflict_of_interest=False,
        ),
    )
