"""Tests for cce.verification.gate — quality gate decision logic."""

import pytest

from cce.models.evidence import SourceQuality
from cce.verification.gate import GateDecision, GateResult, QualityGate
from tests.conftest import (
    make_content_unit,
    make_evidence,
    make_gate_config,
    make_verification_report,
)

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
    evidence: list | None = None,
    **report_overrides,
) -> GateResult:
    """Helper to run gate.evaluate with minimal boilerplate.

    ``evidence`` defaults to the two IDs the default content cites, so every
    marker resolves unless a test says otherwise (B4).
    """
    if evidence is None:
        evidence = [make_evidence(id="test_001"), make_evidence(id="test_002")]
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
    return gate.evaluate(unit, report, iteration, evidence=evidence)


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
    result = _evaluate(
        content=content, confidence_score=0.5, unsupported=1, iteration=1
    )
    assert (
        "Citation density" in result.feedback or "citation" in result.feedback.lower()
    )


def test_gate_feedback_no_issues():
    result = _evaluate(
        confidence_score=0.95,
        unsupported=0,
        uncited=0,
        leakage=0,
        conflicts=0,
    )
    assert result.feedback == "No issues found."


def test_gate_coi_logged_not_in_feedback(caplog):
    """COI is logged as a warning, not mixed into writer feedback."""
    import logging

    config = make_gate_config()
    gate = QualityGate(config)
    unit = make_content_unit(
        content="A paragraph with enough words and a citation [ev:test_001] for the gate."
    )
    report = make_verification_report(confidence_score=0.9)
    evidence = [
        make_evidence(id="ev_clean"),
        make_evidence(
            id="ev_coi",
            source_quality=SourceQuality(conflict_of_interest=True),
        ),
    ]
    with caplog.at_level(logging.WARNING):
        result = gate.evaluate(unit, report, iteration=1, evidence=evidence)
    # COI should NOT be in writer feedback
    assert "conflict of interest" not in result.feedback
    # COI should be logged
    assert any("conflict of interest" in r.message for r in caplog.records)


def test_gate_feedback_no_coi_when_evidence_clean():
    config = make_gate_config()
    gate = QualityGate(config)
    unit = make_content_unit(
        content="A paragraph with enough words and a citation [ev:test_001] for the gate."
    )
    report = make_verification_report(confidence_score=0.95)
    evidence = [make_evidence(id="ev_clean")]
    result = gate.evaluate(unit, report, iteration=1, evidence=evidence)
    assert "conflict of interest" not in result.feedback


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
    report = make_verification_report(unsupported=0, uncited=0, leakage=0, conflicts=0)
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


# ---------------------------------------------------------------------------
# Inline marker resolution (B4)
# ---------------------------------------------------------------------------

# Two substantive paragraphs, three markers: one real, two invented. Dense
# enough that the density check passes on marker count alone.
_PHANTOM_DRAFT = (
    "Sleep hygiene covers the habits that shape a night of rest, and the "
    "evidence here is consistent across sources [ev:ev_real].\n\n"
    "A second paragraph long enough to count as substantive for the density "
    "check cites two sources that were never provided [ev:ev_ghost1] and "
    "[ev:ev_ghost2]."
)


def test_gate_fails_draft_with_invented_markers_and_names_them():
    """B4 acceptance: one real and two invented markers -> FAIL, both named.

    Confidence 1.0, zero leakage and dense markers, so without the marker
    check this draft would PASS — the check is what fails it.
    """
    result = _evaluate(
        content=_PHANTOM_DRAFT,
        confidence_score=1.0,
        evidence=[make_evidence(id="ev_real")],
    )

    assert result.decision == GateDecision.FAIL
    assert "ev_ghost1" in result.feedback
    assert "ev_ghost2" in result.feedback
    assert "ev_real" not in result.feedback


def test_gate_passes_same_draft_when_every_marker_resolves():
    """Control for the acceptance test: same draft, all markers real -> PASS."""
    result = _evaluate(
        content=_PHANTOM_DRAFT,
        confidence_score=1.0,
        evidence=[
            make_evidence(id="ev_real"),
            make_evidence(id="ev_ghost1"),
            make_evidence(id="ev_ghost2"),
        ],
    )

    assert result.decision == GateDecision.PASS


def test_gate_invented_markers_route_to_review_at_max_iterations():
    """At the last iteration an unresolved marker still blocks PASS; it goes to
    human review with the IDs in the feedback."""
    result = _evaluate(
        content=_PHANTOM_DRAFT,
        confidence_score=1.0,
        iteration=3,
        max_writer_iterations=3,
        evidence=[make_evidence(id="ev_real")],
    )

    assert result.decision == GateDecision.REVIEW
    assert "ev_ghost1" in result.feedback
    assert "ev_ghost2" in result.feedback


def test_gate_empty_evidence_leaves_every_marker_unresolved():
    """An empty evidence list is checked, not skipped (no truthiness shortcut)."""
    result = _evaluate(content=_PHANTOM_DRAFT, confidence_score=1.0, evidence=[])

    assert result.decision == GateDecision.FAIL
    for ev_id in ("ev_real", "ev_ghost1", "ev_ghost2"):
        assert ev_id in result.feedback


def test_gate_requires_evidence():
    """Evidence is required: an omitted set would silently skip the check."""
    gate = QualityGate(make_gate_config())
    with pytest.raises(TypeError):
        gate.evaluate(  # type: ignore[call-arg]
            make_content_unit(content=_PHANTOM_DRAFT),
            make_verification_report(confidence_score=1.0),
            1,
        )


@pytest.mark.parametrize(
    "marker",
    ["[ev:ev_abc123]", "[ev_abc123]", "[ev:abc123]"],
    ids=["colon", "bare", "colon-without-prefix"],
)
def test_gate_accepts_every_marker_form_emit_resolves(marker):
    """The gate uses emit's grammar and `ev_` prefix fallback, so the forms
    build_citation_index resolves are accepted here too."""
    content = (
        "This is a long enough paragraph with more than fifteen words so the "
        f"density check applies to it {marker}."
    )
    result = _evaluate(
        content=content,
        confidence_score=1.0,
        evidence=[make_evidence(id="ev_abc123")],
    )

    assert result.decision == GateDecision.PASS


def test_gate_and_emit_agree_on_unresolved_markers():
    """Every marker the gate reports is one emit renders as [^?], and vice
    versa (B4: 'the same set emit will use')."""
    from cce.output.mdx.citations import build_citation_index

    content = (
        "Real [ev:ev_real], unprefixed [ev:real2], bare [ev_real], phantom "
        "[ev:ev_nope], multi [ev:ev_real, ev_nope], and bare phantom [ev_zzz]."
    )
    evidence = [make_evidence(id="ev_real"), make_evidence(id="ev_real2")]
    by_id = {ev.id: ev for ev in evidence}

    unresolved = QualityGate._unresolved_markers(
        make_content_unit(content=content), evidence
    )
    rendered = build_citation_index(content, by_id).content

    assert unresolved == ["ev_nope", "ev_real, ev_nope", "ev_zzz"]
    assert rendered.count("[^?]") == len(unresolved)


def test_gate_multi_id_bracket_gets_its_own_feedback():
    """A bracket holding several valid IDs still blocks PASS (emit renders it
    as [^?]), but the feedback tells the writer to split it rather than
    calling valid IDs unresolved."""
    content = (
        "This paragraph is long enough to be substantive for the density check "
        "and cites two real sources in one bracket [ev_a, ev_b]."
    )
    result = _evaluate(
        content=content,
        confidence_score=1.0,
        evidence=[make_evidence(id="ev_a"), make_evidence(id="ev_b")],
    )

    assert result.decision == GateDecision.FAIL
    assert "[ev_a, ev_b]" in result.feedback
    assert "one marker per source" in result.feedback
    assert "do not resolve" not in result.feedback
