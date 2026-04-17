"""Integration tests for the programmatic scorer inside the Pipeline (M02).

The goal is *behavioral* verification: when a Scorer is wired, every
write-verify iteration should produce a StyleScores on the ContentUnit and a
JobStage.SCORE record; when no scorer is wired, nothing should change versus
pre-humanization behavior.
"""

from __future__ import annotations

import pytest

from cce.config.markers import load_markers
from cce.config.types import HumanizationThresholds
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from cce.synthesis.scoring import Scorer
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    llm as _llm,
)
from tests.test_orchestrator.conftest import (
    make_adapter as _make_adapter,
)
from tests.test_orchestrator.conftest import (
    verifier_json as _verifier_json,
)
from tests.test_orchestrator.conftest import (
    writer_json as _writer_json,
)

pytestmark = pytest.mark.integration


def _make_scorer() -> Scorer:
    """Real marker YAML — integration-level wiring, not a stub."""
    return Scorer(
        thresholds=HumanizationThresholds(),
        markers=load_markers("config/humanization_markers.yaml"),
    )


async def test_pipeline_with_scorer_attaches_style_scores(sqlite_store):
    """Every published ContentUnit carries StyleScores when a scorer is wired."""
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_make_scorer(),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert result.package is not None
    for unit in result.package.units:
        assert unit.style_scores is not None
        assert unit.style_scores.word_count >= 0


async def test_pipeline_with_scorer_records_score_stage(sqlite_store):
    """JobStage.SCORE is appended once per write-verify iteration per path."""
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_make_scorer(),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    score_records = [r for r in result.job.stages if r.stage == JobStage.SCORE]
    assert len(score_records) >= 1
    assert score_records[0].metrics is not None
    assert "humanization_pass" in score_records[0].metrics
    assert "sentence_length_stddev" in score_records[0].metrics
    assert "word_count" in score_records[0].metrics


async def test_pipeline_without_scorer_unchanged(sqlite_store):
    """Omitting scorer → no SCORE records, style_scores stays None."""
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        # scorer=None (default)
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    score_records = [r for r in result.job.stages if r.stage == JobStage.SCORE]
    assert score_records == []
    assert result.package is not None
    for unit in result.package.units:
        assert unit.style_scores is None


async def test_pipeline_scoring_does_not_affect_gate_decision(sqlite_store):
    """ADR-006 regression: citation-only gate decision is byte-identical
    with and without the scorer."""
    config = make_engine_config()
    adapter_a = _make_adapter()
    adapter_b = _make_adapter()
    llm_a = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))
    llm_b = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipe_without = Pipeline(
        config=config, crawl_adapter=adapter_a, evidence_store=sqlite_store, llm=llm_a
    )
    pipe_with = Pipeline(
        config=config,
        crawl_adapter=adapter_b,
        evidence_store=sqlite_store,
        llm=llm_b,
        scorer=_make_scorer(),
    )

    result_without = await pipe_without.run(
        make_curation_request(), make_source_policy()
    )
    result_with = await pipe_with.run(make_curation_request(), make_source_policy())

    decisions_without = [gr.decision for gr in result_without.gate_results]
    decisions_with = [gr.decision for gr in result_with.gate_results]
    assert decisions_without == decisions_with
