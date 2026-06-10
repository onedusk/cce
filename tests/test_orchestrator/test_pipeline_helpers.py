"""Direct unit tests for the M07 pipeline phase helpers (T-07.06).

Pins branches previously reachable only through full Pipeline.run()
integration runs: terminal-decision interpretation (the audit-1.1-era
FAIL-only fix), score/edit phase gating (humanization disabled / scorer
pass / editor citation-drift fallback), draft_source provenance
(finding 1.5), and explicit (path, iteration) grouping invariance
(T-07.05).
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import pytest

from cce.models.content import Citation
from cce.models.job import Job, JobStage, JobStatus, StageRecord
from cce.models.style import StyleScores
from cce.orchestrator.pipeline import (
    Pipeline,
    _per_path_iteration_counts,
    _zero_tokens,
)
from cce.synthesis.editor import EditorOutput
from cce.verification.gate import GateDecision, GateResult
from cce.verification.verifier import VerificationReport
from tests.conftest import (
    MockCrawlAdapter,
    MockLLMProvider,
    make_content_unit,
    make_curation_request,
    make_engine_config,
    make_evidence,
    make_verification_report,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------


def _pipeline(**kwargs) -> Pipeline:
    """Pipeline wired with protocol fakes; the helpers under test do no I/O."""
    return Pipeline(
        config=make_engine_config(),
        crawl_adapter=MockCrawlAdapter(),
        evidence_store=None,  # type: ignore[arg-type] — unused by these helpers
        llm=MockLLMProvider(),
        **kwargs,
    )


def _gate_result(decision: GateDecision, iteration: int = 1) -> GateResult:
    return GateResult(
        decision=decision,
        confidence=0.0,
        coverage=0.0,
        feedback="",
        report=VerificationReport(),
        iteration=iteration,
    )


def _style_scores(*, humanization_pass: bool) -> StyleScores:
    return StyleScores(
        sentence_length_stddev=20.0,
        suppressed_vocab_hits=0,
        type_token_ratio=0.80,
        formulaic_transition_count=0,
        contrastive_frame_count=0,
        hedging_phrase_count=0,
        em_dash_count=0,
        word_count=100,
        humanization_pass=humanization_pass,
    )


class _StubScorer:
    """Protocol-compliant scorer stub with a fixed humanization verdict."""

    def __init__(self, *, humanization_pass: bool) -> None:
        self._pass = humanization_pass

    def score(self, content: str) -> StyleScores:
        return _style_scores(humanization_pass=self._pass)


class _StubEditor:
    """Protocol-compliant editor stub returning a scripted EditorOutput."""

    def __init__(self, output: EditorOutput) -> None:
        self._output = output
        self.calls = 0

    async def edit(self, unit, *, path_config=None, scores=None, annotations=None):
        self.calls += 1
        return self._output


def _editor_output(*, content: str, preserved: bool) -> EditorOutput:
    return EditorOutput(
        edited_content=content,
        notes="",
        citations_preserved=preserved,
        word_count_before=10,
        word_count_after=len(content.split()),
        raw_response="",
        token_usage={"input_tokens": 7, "output_tokens": 3},
    )


def _job() -> Job:
    return Job(id="job_test", request=make_curation_request())


# ---------------------------------------------------------------------------
# _interpret_terminal_decisions — truth table
# ---------------------------------------------------------------------------


class TestInterpretTerminalDecisions:
    def _interpret(self, by_path: dict[str, list[GateResult]]) -> JobStatus:
        pipeline = _pipeline()
        paths = list(by_path)
        flat = [gr for group in by_path.values() for gr in group]
        return pipeline._interpret_terminal_decisions(flat, paths, by_path)

    def test_all_pass_yields_completed(self):
        status = self._interpret(
            {
                "blog": [_gate_result(GateDecision.PASS)],
                "faq": [_gate_result(GateDecision.PASS)],
            }
        )
        assert status == JobStatus.COMPLETED

    def test_any_review_yields_review_required(self):
        status = self._interpret(
            {
                "blog": [_gate_result(GateDecision.PASS)],
                "faq": [_gate_result(GateDecision.REVIEW)],
            }
        )
        assert status == JobStatus.REVIEW_REQUIRED

    def test_fail_only_yields_review_required(self):
        """Pins the audit-1.1-era fix: FAIL with no REVIEW must not COMPLETE."""
        status = self._interpret(
            {
                "blog": [_gate_result(GateDecision.FAIL)],
                "faq": [_gate_result(GateDecision.FAIL)],
            }
        )
        assert status == JobStatus.REVIEW_REQUIRED

    def test_mixed_pass_and_fail_yields_review_required(self):
        status = self._interpret(
            {
                "blog": [_gate_result(GateDecision.PASS)],
                "faq": [_gate_result(GateDecision.FAIL)],
            }
        )
        assert status == JobStatus.REVIEW_REQUIRED

    def test_terminal_decision_uses_max_iteration_not_list_order(self):
        """T-07.05: within a path, the highest iteration is terminal even
        when the group's list order is shuffled."""
        status = self._interpret(
            {
                "blog": [
                    _gate_result(GateDecision.PASS, iteration=2),
                    _gate_result(GateDecision.FAIL, iteration=1),
                ],
            }
        )
        assert status == JobStatus.COMPLETED

    def test_empty_path_group_is_terminal_fail(self):
        """A path whose writer produced no content contributes zero gate
        results — terminal FAIL, so the job routes to review."""
        status = self._interpret(
            {
                "blog": [_gate_result(GateDecision.PASS)],
                "faq": [],
            }
        )
        assert status == JobStatus.REVIEW_REQUIRED

    def test_flat_list_fallback_without_by_path(self):
        """Direct callers without the by-path mapping use the legacy
        iteration-boundary partitioning."""
        pipeline = _pipeline()
        status = pipeline._interpret_terminal_decisions(
            [_gate_result(GateDecision.PASS)], ["blog"]
        )
        assert status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# _score_draft — humanization gating branches
# ---------------------------------------------------------------------------


class TestScoreDraft:
    def test_humanization_disabled_is_noop(self):
        """scorer=None → unit unchanged, no style scores, no SCORE record."""
        pipeline = _pipeline()
        unit = make_content_unit()
        job = _job()

        scored, style = pipeline._score_draft(unit, "blog", job)

        assert scored is unit
        assert style is None
        assert job.stages == []

    def test_scorer_pass_records_score_stage(self):
        pipeline = _pipeline(scorer=_StubScorer(humanization_pass=True))  # type: ignore[arg-type]
        unit = make_content_unit()
        job = _job()

        scored, style = pipeline._score_draft(unit, "blog", job)

        assert style is not None and style.humanization_pass is True
        assert scored.style_scores == style
        [rec] = job.stages
        assert rec.stage == JobStage.SCORE
        assert rec.path == "blog"
        assert rec.metrics is not None
        assert rec.metrics["path"] == "blog"
        assert rec.metrics["humanization_pass"] is True

    def test_scorer_runs_without_job_record(self):
        """job=None (direct loop invocation outside the full pipeline)
        still scores but appends nothing."""
        pipeline = _pipeline(scorer=_StubScorer(humanization_pass=False))  # type: ignore[arg-type]
        unit = make_content_unit()

        scored, style = pipeline._score_draft(unit, "blog", None)

        assert style is not None and style.humanization_pass is False
        assert scored.style_scores == style


# ---------------------------------------------------------------------------
# _edit_draft — editor success vs citation-drift fallback (finding 1.5)
# ---------------------------------------------------------------------------


class TestEditDraft:
    async def _edit(
        self,
        *,
        preserved: bool,
        content: str = "Rewritten [ev:test_001].",
        token_usage: dict | None = None,
    ):
        editor = _StubEditor(_editor_output(content=content, preserved=preserved))
        pipeline = _pipeline(editor=editor)  # type: ignore[arg-type]
        unit = make_content_unit()
        job = _job()
        edited = await pipeline._edit_draft(
            unit,
            style_scores=_style_scores(humanization_pass=False),
            path_evidence=[make_evidence()],
            path_config=None,
            path="blog",
            iteration=1,
            job=job,
            token_usage=token_usage,
            log=logging.getLogger("test_pipeline_helpers"),
        )
        return unit, edited, job

    async def test_editor_success_sets_draft_source_editor(self):
        original, edited, job = await self._edit(preserved=True)

        assert edited.content == "Rewritten [ev:test_001]."
        assert edited.draft_source == "editor"
        [rec] = job.stages
        assert rec.stage == JobStage.EDIT
        assert rec.path == "blog"
        assert rec.metrics is not None
        assert rec.metrics["citations_preserved"] is True

    async def test_citation_drift_falls_back_to_writer_draft(self):
        """Editor output dropped a citation → the writer's draft is retained
        and draft_source stays 'writer'."""
        original, edited, job = await self._edit(
            preserved=False, content="Rewrite without the marker."
        )

        assert edited is original
        assert edited.draft_source == "writer"
        assert "without the marker" not in edited.content
        [rec] = job.stages
        assert rec.stage == JobStage.EDIT
        assert rec.metrics is not None
        assert rec.metrics["citations_preserved"] is False

    async def test_editor_tokens_thread_into_per_path_dict(self):
        """Token accumulation threads the per-path dict (audit P1 pattern)."""
        tokens = _zero_tokens()
        await self._edit(preserved=True, token_usage=tokens)

        assert tokens["input_tokens"] == 7
        assert tokens["output_tokens"] == 3


# ---------------------------------------------------------------------------
# _apply_verification_scores — provenance survives the score update
# ---------------------------------------------------------------------------


class TestApplyVerificationScores:
    def test_preserves_draft_source_and_aggregates_tags(self):
        """The pre-M07 9-field ContentUnit reconstruction would have reset
        draft_source to its default; model_copy + with_scores must not."""
        ev = make_evidence(tags=["sleep", "memory"])
        unit = make_content_unit(
            citations=[Citation(evidence_id=ev.id, url=ev.url)],
            draft_source="editor",
            style_scores=_style_scores(humanization_pass=True),
        )
        report = make_verification_report()

        result = Pipeline._apply_verification_scores(unit, report, {ev.id: ev})

        assert result.draft_source == "editor"
        assert result.style_scores == unit.style_scores
        assert result.tags == ["memory", "sleep"]
        assert result.scores.confidence == report.confidence_score
        assert result.scores.coverage == report.pass_rate
        assert result.scores.source_diversity == unit.scores.source_diversity


# ---------------------------------------------------------------------------
# _per_path_iteration_counts — explicit grouping, order invariance (T-07.05)
# ---------------------------------------------------------------------------


def _write_record(path: str, iteration: int) -> StageRecord:
    return StageRecord(
        stage=JobStage.WRITE,
        path=path,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        metrics={"path": path, "iterations": iteration},
    )


class TestPerPathIterationCountsGrouping:
    def test_stage_order_shuffle_does_not_change_output(self):
        records = [
            _write_record(p, i)
            for p, n in (("a", 2), ("b", 3), ("c", 1))
            for i in range(1, n + 1)
        ]
        rng = random.Random(1337)

        for _ in range(10):
            rng.shuffle(records)
            job = Job(id="j1", request=make_curation_request(paths=["a", "b", "c"]))
            job.stages.extend(records)
            assert _per_path_iteration_counts(job, ["a", "b", "c"]) == [2, 3, 1]

    def test_metrics_path_fallback_for_pre_m07_records(self):
        """Records stored before StageRecord.path existed group via
        metrics['path']."""
        legacy = StageRecord(
            stage=JobStage.WRITE,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            metrics={"path": "a", "iterations": 4},
        )
        job = Job(id="j1", request=make_curation_request(paths=["a"]))
        job.stages.append(legacy)

        assert legacy.path is None
        assert _per_path_iteration_counts(job, ["a"]) == [4]
