"""Tests for cce.verification.gate — quality gate decision logic."""

import pytest

from cce.verification.gate import GateDecision, GateResult, QualityGate
from tests.conftest import make_content_unit, make_gate_config, make_verification_report

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evaluate(
    *,
    confidence_score: float = 0.9,
    unsupported: int = 0,
    uncited: int = 0,
    leakage: int = 0,
    conflicts: int = 0,
    iteration: int = 1,
    content: str = "This is a long enough paragraph with a citation [ev:test_001] to pass density checks easily.\n\nAnother paragraph with enough words and a citation [ev:test_002] for the gate.",
    autopublish_threshold: float = 0.85,
    max_writer_iterations: int = 3,
    min_citations_per_paragraph: int = 1,
    **report_overrides,
) -> GateResult:
    """Helper to run gate.evaluate with minimal boilerplate."""
    config = make_gate_config(
        autopublish_threshold=autopublish_threshold,
        max_writer_iterations=max_writer_iterations,
        min_citations_per_paragraph=min_citations_per_paragraph,
    )
    gate = QualityGate(config)
    unit = make_content_unit(content=content)
    report = make_verification_report(
        confidence_score=confidence_score,
        unsupported=unsupported,
        uncited=uncited,
        leakage=leakage,
        conflicts=conflicts,
        **report_overrides,
    )
    return gate.evaluate(unit, report, iteration)


# ---------------------------------------------------------------------------
# Decision routing
# ---------------------------------------------------------------------------


def test_gate_pass_high_confidence():
    result = _evaluate(confidence_score=0.9, leakage=0)
    assert result.decision == GateDecision.PASS


def test_gate_fail_low_confidence_fixable():
    result = _evaluate(confidence_score=0.5, unsupported=3, iteration=1)
    assert result.decision == GateDecision.FAIL


def test_gate_review_max_iterations():
    result = _evaluate(confidence_score=0.5, unsupported=3, iteration=3)
    assert result.decision == GateDecision.REVIEW
    assert "Max iterations" in result.feedback


def test_gate_review_no_fixable_issues():
    # Low confidence but nothing the writer can fix
    result = _evaluate(
        confidence_score=0.5,
        unsupported=0,
        uncited=0,
        leakage=0,
        conflicts=0,
        iteration=1,
    )
    assert result.decision == GateDecision.REVIEW


def test_gate_fail_leakage_blocks_pass():
    result = _evaluate(confidence_score=0.95, leakage=1, iteration=1)
    assert result.decision != GateDecision.PASS


# ---------------------------------------------------------------------------
# Feedback generation
# ---------------------------------------------------------------------------


def test_gate_feedback_unsupported():
    result = _evaluate(unsupported=3)
    assert "3" in result.feedback
    assert "don't match" in result.feedback


def test_gate_feedback_uncited():
    result = _evaluate(uncited=2)
    assert "2" in result.feedback
    assert "no citations" in result.feedback


def test_gate_feedback_leakage():
    result = _evaluate(leakage=1)
    assert "1" in result.feedback
    assert "training data" in result.feedback


def test_gate_feedback_conflicts():
    result = _evaluate(conflicts=2)
    assert "2" in result.feedback
    assert "contradiction" in result.feedback


def test_gate_feedback_citation_density():
    # Paragraph with no citations — should trigger density feedback
    content = (
        "This is a substantive paragraph with more than fifteen words "
        "but it does not contain any evidence citations at all anywhere."
    )
    result = _evaluate(content=content, confidence_score=0.5, unsupported=1, iteration=1)
    assert "Citation density" in result.feedback or "citation" in result.feedback.lower()


def test_gate_feedback_no_issues():
    result = _evaluate(
        confidence_score=0.95,
        unsupported=0,
        uncited=0,
        leakage=0,
        conflicts=0,
    )
    assert result.feedback == "No issues found."


# ---------------------------------------------------------------------------
# _check_citation_density
# ---------------------------------------------------------------------------


def test_check_citation_density_empty():
    gate = QualityGate(make_gate_config())
    unit = make_content_unit(content="")
    ok, ratio = gate._check_citation_density(unit)
    # Empty content returns (False, 0.0) — not vacuously true
    assert ok is False
    assert ratio == 0.0


def test_check_citation_density_short_paragraphs_skipped():
    gate = QualityGate(make_gate_config())
    # Paragraph with <=15 words — not substantive, should be skipped
    unit = make_content_unit(content="Short paragraph here.")
    ok, ratio = gate._check_citation_density(unit)
    # No substantive paragraphs → vacuously true
    assert ok is True
    assert ratio == 1.0


def test_check_citation_density_headings_skipped():
    gate = QualityGate(make_gate_config())
    content = (
        "# This is a heading that should be skipped\n\n"
        "This is a substantive paragraph with more than fifteen words "
        "and it has a citation [ev:test_001] which should satisfy the gate."
    )
    unit = make_content_unit(content=content)
    ok, ratio = gate._check_citation_density(unit)
    assert ok is True
    assert ratio == 1.0


def test_check_citation_density_ev_colon_format():
    gate = QualityGate(make_gate_config())
    content = (
        "This is a substantive paragraph with more than fifteen words "
        "and it references evidence using the colon format [ev:abc123] here."
    )
    unit = make_content_unit(content=content)
    ok, _ = gate._check_citation_density(unit)
    assert ok is True


def test_check_citation_density_ev_underscore_format():
    gate = QualityGate(make_gate_config())
    content = (
        "This is a substantive paragraph with more than fifteen words "
        "and it references evidence using the underscore format [ev_abc123] here."
    )
    unit = make_content_unit(content=content)
    ok, _ = gate._check_citation_density(unit)
    assert ok is True


def test_check_citation_density_multiple_required():
    gate = QualityGate(make_gate_config(min_citations_per_paragraph=2))
    # Paragraph with 1 citation — fails when 2 required
    content_one = (
        "This is a substantive paragraph with more than fifteen words "
        "and it has only one citation [ev:test_001] which is not enough."
    )
    unit = make_content_unit(content=content_one)
    ok, ratio = gate._check_citation_density(unit)
    assert ok is False
    assert ratio == 0.0

    # Paragraph with 2 citations — passes
    content_two = (
        "This is a substantive paragraph with more than fifteen words "
        "and it has two citations [ev:test_001] and also [ev:test_002] here."
    )
    unit = make_content_unit(content=content_two)
    ok, ratio = gate._check_citation_density(unit)
    assert ok is True
    assert ratio == 1.0


# ---------------------------------------------------------------------------
# _has_fixable_issues
# ---------------------------------------------------------------------------


def test_has_fixable_issues_true():
    for field in ["unsupported", "uncited", "leakage", "conflicts"]:
        report = make_verification_report(**{field: 1})
        assert QualityGate._has_fixable_issues(report) is True, f"Failed for {field}"


def test_has_fixable_issues_false():
    report = make_verification_report(
        unsupported=0, uncited=0, leakage=0, conflicts=0
    )
    assert QualityGate._has_fixable_issues(report) is False


# ---------------------------------------------------------------------------
# GateResult properties
# ---------------------------------------------------------------------------


def test_gate_result_properties():
    report = make_verification_report()

    pass_result = GateResult(
        decision=GateDecision.PASS,
        confidence=0.9,
        coverage=0.8,
        feedback="",
        report=report,
        iteration=1,
    )
    assert pass_result.should_publish is True
    assert pass_result.should_rewrite is False
    assert pass_result.needs_human is False

    fail_result = GateResult(
        decision=GateDecision.FAIL,
        confidence=0.5,
        coverage=0.6,
        feedback="fix it",
        report=report,
        iteration=1,
    )
    assert fail_result.should_rewrite is True
    assert fail_result.should_publish is False
    assert fail_result.needs_human is False

    review_result = GateResult(
        decision=GateDecision.REVIEW,
        confidence=0.5,
        coverage=0.4,
        feedback="needs review",
        report=report,
        iteration=3,
    )
    assert review_result.needs_human is True
    assert review_result.should_publish is False
    assert review_result.should_rewrite is False
