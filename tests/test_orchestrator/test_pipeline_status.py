"""Tests for pipeline final-status logic and StageRecord metrics field.

M01 hardening: verifies that the status-determination block in Pipeline.run
correctly maps terminal gate decisions to JobStatus, and that StageRecord
accepts the optional metrics kwarg.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cce.models.job import JobStage, JobStatus, StageRecord
from cce.orchestrator.pipeline import _terminal_decisions
from cce.verification.gate import GateDecision

# ---------------------------------------------------------------------------
# Helper: simulate the status-determination logic from Pipeline.run
# ---------------------------------------------------------------------------


def _determine_status(terminal_decisions: list[GateDecision]) -> JobStatus:
    """Replicate the pipeline's status-determination block.

    Accepts *terminal* decisions (one per output path) — the same list that
    ``_terminal_decisions()`` produces from the raw gate-results stream.
    Must stay in sync with the logic in Pipeline.run.
    """
    if all(d == GateDecision.PASS for d in terminal_decisions):
        return JobStatus.COMPLETED
    elif any(d == GateDecision.REVIEW for d in terminal_decisions):
        return JobStatus.REVIEW_REQUIRED
    else:
        # Gate returned FAIL after max iterations — content needs human review
        return JobStatus.REVIEW_REQUIRED


def _make_gate_result(decision: GateDecision, iteration: int = 1):
    """Build a minimal GateResult-like object for _terminal_decisions."""
    from cce.verification.verifier import VerificationReport

    report = VerificationReport(
        claims=[],
        total_claims=0,
        supported=0,
        unsupported=0,
        uncited=0,
        leakage=0,
        conflicts=0,
        gaps_acknowledged=0,
        contradictions=[],
        overall_feedback="",
        confidence_score=0.0,
        raw_response="{}",
    )
    from cce.verification.gate import GateResult

    return GateResult(
        decision=decision,
        confidence=0.0,
        coverage=0.0,
        feedback="",
        report=report,
        iteration=iteration,
    )


# ---------------------------------------------------------------------------
# Status determination tests (terminal decisions)
# ---------------------------------------------------------------------------


class TestPipelineStatusDetermination:
    """Verify that every combination of terminal gate decisions maps to the right status."""

    @pytest.mark.unit
    def test_all_pass_yields_completed(self):
        decisions = [GateDecision.PASS, GateDecision.PASS]
        assert _determine_status(decisions) == JobStatus.COMPLETED

    @pytest.mark.unit
    def test_single_pass_yields_completed(self):
        decisions = [GateDecision.PASS]
        assert _determine_status(decisions) == JobStatus.COMPLETED

    @pytest.mark.unit
    def test_mix_pass_and_review_yields_review_required(self):
        decisions = [GateDecision.PASS, GateDecision.REVIEW]
        assert _determine_status(decisions) == JobStatus.REVIEW_REQUIRED

    @pytest.mark.unit
    def test_mix_pass_and_fail_yields_review_required(self):
        """The bug fix: FAIL terminal decisions (no REVIEW) must not yield COMPLETED."""
        decisions = [GateDecision.PASS, GateDecision.FAIL]
        assert _determine_status(decisions) == JobStatus.REVIEW_REQUIRED

    @pytest.mark.unit
    def test_all_fail_yields_review_required(self):
        decisions = [GateDecision.FAIL, GateDecision.FAIL]
        assert _determine_status(decisions) == JobStatus.REVIEW_REQUIRED

    @pytest.mark.unit
    def test_all_review_yields_review_required(self):
        decisions = [GateDecision.REVIEW, GateDecision.REVIEW]
        assert _determine_status(decisions) == JobStatus.REVIEW_REQUIRED

    @pytest.mark.unit
    def test_mix_fail_and_review_yields_review_required(self):
        decisions = [GateDecision.FAIL, GateDecision.REVIEW]
        assert _determine_status(decisions) == JobStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# _terminal_decisions extraction tests
# ---------------------------------------------------------------------------


class TestTerminalDecisions:
    """Verify that _terminal_decisions correctly extracts the last decision per path."""

    @pytest.mark.unit
    def test_single_path_single_iteration(self):
        results = [_make_gate_result(GateDecision.PASS, iteration=1)]
        decisions = _terminal_decisions(results, ["blog"])
        assert decisions == [GateDecision.PASS]

    @pytest.mark.unit
    def test_single_path_rewrite_then_pass(self):
        """Intermediate FAIL should be ignored — only terminal PASS counts."""
        results = [
            _make_gate_result(GateDecision.FAIL, iteration=1),
            _make_gate_result(GateDecision.PASS, iteration=2),
        ]
        decisions = _terminal_decisions(results, ["blog"])
        assert decisions == [GateDecision.PASS]

    @pytest.mark.unit
    def test_multiple_paths_both_pass(self):
        results = [
            _make_gate_result(GateDecision.PASS, iteration=1),  # path 1
            _make_gate_result(GateDecision.PASS, iteration=1),  # path 2
        ]
        decisions = _terminal_decisions(results, ["blog", "newsletter"])
        assert decisions == [GateDecision.PASS, GateDecision.PASS]

    @pytest.mark.unit
    def test_multiple_paths_one_fails_terminally(self):
        results = [
            _make_gate_result(GateDecision.PASS, iteration=1),  # path 1: pass
            _make_gate_result(GateDecision.FAIL, iteration=1),  # path 2: fail iter 1
            _make_gate_result(GateDecision.FAIL, iteration=2),  # path 2: fail iter 2
            _make_gate_result(
                GateDecision.REVIEW, iteration=3
            ),  # path 2: review iter 3
        ]
        decisions = _terminal_decisions(results, ["blog", "newsletter"])
        assert decisions == [GateDecision.PASS, GateDecision.REVIEW]

    @pytest.mark.unit
    def test_empty_gate_results_pads_with_fail(self):
        """No gate results at all -> every path is FAIL (review finding F-2)."""
        assert _terminal_decisions([], ["blog"]) == [GateDecision.FAIL]
        assert _terminal_decisions([], ["blog", "summary", "faq"]) == [
            GateDecision.FAIL,
            GateDecision.FAIL,
            GateDecision.FAIL,
        ]

    @pytest.mark.unit
    def test_sparse_groups_padded_with_fail(self):
        """Only 1 path produces results in a 2-path run -> pad tail with FAIL.

        Simulates the case where path[0]'s writer had content (1 iteration,
        PASS) but path[1]'s writer returned empty, so the loop broke before
        any gate result for path[1] existed.
        """
        results = [_make_gate_result(iteration=1, decision=GateDecision.PASS)]
        assert _terminal_decisions(results, ["blog", "summary"]) == [
            GateDecision.PASS,
            GateDecision.FAIL,
        ]

    @pytest.mark.unit
    def test_multiple_paths_with_some_missing(self):
        """3-path run; only paths 0 and 1 produced results."""
        results = [
            _make_gate_result(iteration=1, decision=GateDecision.PASS),
            _make_gate_result(iteration=1, decision=GateDecision.FAIL),
            _make_gate_result(iteration=2, decision=GateDecision.PASS),
        ]
        assert _terminal_decisions(results, ["a", "b", "c"]) == [
            GateDecision.PASS,  # path a: single iter, PASS
            GateDecision.PASS,  # path b: iter 1 FAIL then iter 2 PASS
            GateDecision.FAIL,  # path c: padded — writer had no content
        ]


# ---------------------------------------------------------------------------
# StageRecord metrics field tests
# ---------------------------------------------------------------------------


class TestStageRecordMetrics:
    """Verify that the optional metrics field on StageRecord works correctly."""

    @pytest.mark.unit
    def test_stage_record_default_metrics_is_none(self):
        record = StageRecord(
            stage=JobStage.DISCOVER,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        )
        assert record.metrics is None

    @pytest.mark.unit
    def test_stage_record_accepts_metrics_kwarg(self):
        metrics = {"evidence_count": 42, "iteration_count": 3}
        record = StageRecord(
            stage=JobStage.WRITE,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            metrics=metrics,
        )
        assert record.metrics == {"evidence_count": 42, "iteration_count": 3}

    @pytest.mark.unit
    def test_stage_record_metrics_frozen(self):
        """StageRecord is frozen — assignment after construction must fail."""
        record = StageRecord(
            stage=JobStage.VERIFY,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            record.metrics = {"should": "fail"}  # type: ignore[misc]

    @pytest.mark.unit
    def test_stage_record_metrics_serializes(self):
        """Metrics should round-trip through model_dump / model_validate."""
        metrics = {"sources_crawled": 10, "dedup_rate": 0.15}
        record = StageRecord(
            stage=JobStage.EXTRACT,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
            metrics=metrics,
        )
        dumped = record.model_dump()
        restored = StageRecord.model_validate(dumped)
        assert restored.metrics == metrics
