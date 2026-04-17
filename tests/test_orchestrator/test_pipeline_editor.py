"""Integration tests for the Editor inside the Pipeline (humanization M03).

The Editor must:
- Fire only when the Scorer flagged the draft (``humanization_pass=False``).
- Stay invisible when the Scorer passed.
- Not consume an iteration slot — exhausting iterations still hits exactly
  ``max_writer_iterations`` writer calls (ADR-005).
- Fall back to the writer's draft when its rewrite breaks citations.
"""

from __future__ import annotations

import json

import pytest

from cce.config.markers import load_markers
from cce.config.types import EditorConfig, HumanizationThresholds
from cce.llm.base import LLMResponse
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from cce.synthesis.editor import Editor
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


def _scorer() -> Scorer:
    return Scorer(
        thresholds=HumanizationThresholds(),
        markers=load_markers("config/humanization_markers.yaml"),
    )


def _ai_flat_writer_json() -> str:
    """Writer output that the scorer reliably flags as failing."""
    body = (
        "Furthermore, sleep is crucial [ev:ev_001]. "
        "Additionally, sleep showcases robust effects [ev:ev_001]. "
        "Moreover, the comprehensive landscape of sleep is multifaceted [ev:ev_001]. "
        "In conclusion, the pivotal role of sleep is underscored [ev:ev_001]."
    )
    return json.dumps(
        {
            "content": body,
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "Sleep matters", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


def _human_writer_json() -> str:
    """Writer output that passes the scorer (same fixture as the scorer unit test)."""
    body = (
        "Sleep breaks down into stages [ev:ev_001]. Each one does different work — "
        "some repair the body, some consolidate memory, some don't seem to do much "
        "we can name. Wake someone mid-REM and they'll report vivid dreams.\n\n"
        "CBT-I targets the habits that keep people awake [ev:ev_001]. It works. "
        "Six to eight sessions, usually, with homework between. The pattern shows "
        "up in the research: people who finish the course sleep better a year later, "
        "two years later, five years later, which is more than you can say for most "
        "sleep medications. Medication can still help during an acute episode, but "
        "it's not a long-term strategy on its own.\n\n"
        "Why does this work? Because the habits are load-bearing. You fix the habits, "
        "the sleep follows. Short feedback loop, measurable outcome."
    )
    return json.dumps(
        {
            "content": body,
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "Sleep stages", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


def _editor_response(content: str, notes: str = "rewritten") -> str:
    return json.dumps({"edited_content": content, "notes": notes})


def _llm(*scripted: str) -> MockLLMProvider:
    return MockLLMProvider(
        [LLMResponse(content=s, model="mock", stop_reason="end_turn") for s in scripted]
    )


def _editor(llm: MockLLMProvider) -> Editor:
    return Editor(llm=llm, config=EditorConfig(enabled=True))


# ---------------------------------------------------------------------------


async def test_editor_invoked_when_score_fails(sqlite_store):
    """AI-flat writer output → scorer fails → editor rewrites and the
    rewritten content lands in the published unit."""
    config = make_engine_config()
    adapter = _make_adapter()
    rewritten = (
        "Sleep matters [ev:ev_001]. The research is consistent: well-rested people "
        "remember more, perform better, and feel happier across long studies. Short "
        "feedback loop, measurable outcome."
    )
    llm = _llm(
        _ai_flat_writer_json(),
        _editor_response(rewritten),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    assert len(edit_records) == 1
    assert edit_records[0].metrics is not None
    assert edit_records[0].metrics["citations_preserved"] is True
    assert result.package is not None
    assert "research is consistent" in result.package.units[0].content


async def test_editor_skipped_when_score_passes(sqlite_store):
    """Scorer passes → no editor invocation regardless of downstream gate routing.

    Uses the human-bursty body fixture independently verified to pass the
    scorer (humanization_pass=True). The point of the assertion is the
    *absence* of any JobStage.EDIT record — the gate's PASS/REVIEW decision
    is irrelevant to whether the editor fired.
    """
    config = make_engine_config()
    adapter = _make_adapter()
    # Multiple verifier responses to cover potential rewrite iterations
    llm = _llm(
        _human_writer_json(),
        _verifier_json(supported=10, total=10, gaps=0),
        _human_writer_json(),
        _verifier_json(supported=10, total=10, gaps=0),
        _human_writer_json(),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    score_records = [r for r in result.job.stages if r.stage == JobStage.SCORE]

    # Scorer ran and every iteration passed
    assert score_records, "scorer should have produced at least one SCORE record"
    assert all(
        r.metrics is not None and r.metrics["humanization_pass"] is True
        for r in score_records
    )
    # Editor never fired across any iteration
    assert edit_records == []


async def test_editor_failure_falls_back_to_writer_draft(sqlite_store):
    """Editor returns content with a dropped citation → writer's draft is retained
    for the verifier; EDIT record shows citations_preserved=False."""
    config = make_engine_config()
    adapter = _make_adapter()
    # Editor "rewrites" without the [ev:ev_001] marker
    bad_rewrite = "A short rewritten paragraph without the marker."
    llm = _llm(
        _ai_flat_writer_json(),
        _editor_response(bad_rewrite),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    assert len(edit_records) == 1
    assert edit_records[0].metrics["citations_preserved"] is False
    # Writer's draft was retained — original AI-flat content, not the bad rewrite
    assert result.package is not None
    assert "Furthermore" in result.package.units[0].content
    assert "without the marker" not in result.package.units[0].content


async def test_editor_does_not_extend_iteration_count(sqlite_store):
    """ADR-005 regression: editor invocation does not advance the iteration
    counter. With max_writer_iterations=2 and editor firing each iteration,
    the loop still hits exactly 2 writer calls before exiting."""
    config = make_engine_config()
    # Force max_writer_iterations to 2 across all profiles
    for profile in config.quality_gate.values():
        profile.max_writer_iterations = 2
    adapter = _make_adapter()

    # Round 1: AI-flat writer + bad-citation editor + FAIL verifier
    # Round 2: AI-flat writer + bad-citation editor + FAIL verifier (exhausts iter cap)
    llm = _llm(
        _ai_flat_writer_json(),
        _editor_response("rewritten"),  # no [ev:] markers → drift, fallback
        _verifier_json(
            supported=2, total=10, unsupported=8, leakage=0
        ),  # FAIL, fixable
        _ai_flat_writer_json(),
        _editor_response("rewritten again"),
        _verifier_json(supported=2, total=10, unsupported=8, leakage=0),
    )

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    write_records = [r for r in result.job.stages if r.stage == JobStage.WRITE]
    edit_records = [r for r in result.job.stages if r.stage == JobStage.EDIT]
    assert len(write_records) == 2  # exactly max_writer_iterations
    assert len(edit_records) == 2  # editor fired each iteration


async def test_editor_disabled_at_factory_level(monkeypatch, tmp_path):
    """_build_pipeline returns editor=None when editor.enabled is False even
    if the master humanization switch is on (double-gate)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-dummy")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy")

    from cce.api.app import _build_pipeline
    from cce.config.types import (
        CrawlConfig,
        EngineConfig,
        EvidenceStoreConfig,
        HumanizationConfig,
        LLMConfig,
    )
    from cce.evidence.sqlite import SQLiteEvidenceStore

    store = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / "ev.db"))
    await store.connect()

    # FirecrawlAdapter passes config.crawl.api_key explicitly so the env-var
    # fallback in the SDK doesn't kick in — supply the key in the config.
    crawl = CrawlConfig(api_key="test-dummy")

    cfg_off = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(enabled=False),
    )
    pipe_off = _build_pipeline(cfg_off, store)
    assert pipe_off._editor is None
    assert pipe_off._scorer is None

    cfg_master_only = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(enabled=True),
    )
    pipe_master_only = _build_pipeline(cfg_master_only, store)
    assert pipe_master_only._scorer is not None
    assert pipe_master_only._editor is None  # editor.enabled defaults False

    cfg_both = EngineConfig(
        llm=LLMConfig(api_key="test"),
        crawl=crawl,
        humanization=HumanizationConfig(
            enabled=True, editor=EditorConfig(enabled=True)
        ),
    )
    pipe_both = _build_pipeline(cfg_both, store)
    assert pipe_both._scorer is not None
    assert pipe_both._editor is not None
