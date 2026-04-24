"""Tests for the programmatic style scorer (humanization M02)."""

from __future__ import annotations

import pytest

from cce.config.markers import HumanizationMarkers, load_markers
from cce.config.types import HumanizationThresholds
from cce.synthesis.scoring import Scorer

pytestmark = pytest.mark.unit


@pytest.fixture
def markers() -> HumanizationMarkers:
    """Real marker YAML from config/ — integration with the shipped lists."""
    return load_markers("config/humanization_markers.yaml")


@pytest.fixture
def thresholds() -> HumanizationThresholds:
    return HumanizationThresholds()


@pytest.fixture
def scorer(thresholds, markers) -> Scorer:
    return Scorer(thresholds=thresholds, markers=markers)


def test_score_empty_content_passes(scorer):
    """Empty content cannot fail style — the verifier owns emptiness checks."""
    scores = scorer.score("")

    assert scores.word_count == 0
    assert scores.humanization_pass is True
    assert scores.type_token_ratio == 0.0


def test_score_strips_citations_from_word_count(scorer):
    """Citation markers must not count as words or perturb the metrics."""
    without = scorer.score(
        "Sleep fragmentation affects memory consolidation across nights."
    )
    with_cites = scorer.score(
        "Sleep fragmentation [ev:abc123] affects memory consolidation [ev:def456] "
        "across nights [ev:ghi789]."
    )

    assert without.word_count == with_cites.word_count
    # Same tokens, so TTR should match exactly
    assert without.type_token_ratio == with_cites.type_token_ratio


def test_score_ai_flat_sample_fails(scorer):
    """Uniform sentence length + formulaic openings + suppressed vocab ->
    humanization_pass is False."""
    body = "\n\n".join(
        [
            (
                "Furthermore, stress impacts sleep quality significantly. "
                "Additionally, sleep quality affects cognitive performance directly. "
                "Moreover, cognitive performance influences daily productivity."
            ),
            (
                "Additionally, the research showcasing comprehensive results "
                "underscores the pivotal role of sleep. Furthermore, the "
                "multifaceted landscape of circadian rhythms is crucial."
            ),
            (
                "In conclusion, robust sleep habits are crucial for performance. "
                "Additionally, meticulous tracking showcases the intricate landscape."
            ),
        ]
    )

    scores = scorer.score(body)

    # Should register multiple transition hits (paragraph openings) and
    # many suppressed vocab tokens
    assert scores.formulaic_transition_count >= 2
    assert scores.suppressed_vocab_hits >= 5
    assert scores.humanization_pass is False


def test_score_human_bursty_sample_passes(scorer):
    """Mixed sentence lengths, organic transitions, declarative claims -> pass."""
    # Note: no em dashes — the calibrated default threshold is 4.0/1000,
    # which a 166-word fixture cannot satisfy with even one em dash.
    body = (
        "It works. Six to eight sessions, usually, with homework between, aimed "
        "at the habits and thought patterns that keep a person awake at the exact "
        "moment they want most to sleep. The pattern holds up through long "
        "follow-up studies running a year out, two years out, five years out, "
        "which is frankly more than can be said of most sleep medications whose "
        "effects tend to wane once the body adapts. Worth knowing.\n\n"
        "Sleep is layered. REM, deep, light, all doing different work: repair, "
        "memory consolidation, things we still cannot name with any precision. "
        "Wake someone mid-REM and they will describe a vivid dream in detail "
        "that evaporates after a minute or two of full consciousness returning. "
        "Why?\n\n"
        "Because the habits are load-bearing. Fix the habits, the sleep follows "
        "quietly and without ceremony. Short feedback loop, measurable outcome, "
        "and the gains compound quarter after quarter in a way that medications "
        "alone simply do not replicate for most chronic insomnia patients over "
        "the long term. No magic."
    )

    scores = scorer.score(body)

    assert scores.word_count > 100
    # Fixture clears the calibrated defaults (stddev >= 10.0, TTR >= 0.38).
    assert scores.sentence_length_stddev >= 10.0
    assert scores.type_token_ratio >= 0.38
    assert scores.humanization_pass is True


def test_score_density_uses_per_1000_normalization(thresholds, markers):
    """The ``max_hedging_density_per_1000`` threshold should be density-based,
    not absolute."""
    scorer = Scorer(thresholds=thresholds, markers=markers)

    # 500-word body with 2 hedging hits = 4 per 1000; below the 8 threshold.
    base = "Quality sleep supports health. " * 100  # ~300 words of filler
    body_low = (
        base
        + " It is important to note that sleep matters for learning and memory. "
        + "It should be noted that this is grounded in the evidence. "
    )
    scores_low = scorer.score(body_low)
    assert scores_low.hedging_phrase_count == 2
    # 2 hits / word_count * 1000 should be <= 8
    assert scores_low.density_per_1000(scores_low.hedging_phrase_count) <= 8.0


def test_score_contrastive_pattern_matches(scorer):
    """'Unlike X,' should register at least one contrastive frame."""
    body = (
        "CBT-I works through behavioral change. Unlike sleeping pills, it does not "
        "lose effect over time. The research is consistent on this point."
    )
    scores = scorer.score(body)

    assert scores.contrastive_frame_count >= 1


def test_score_subtype_split_parasitic_vs_alternative(scorer):
    """Parasitic and genuine-alternative matches are counted into their
    respective subtype fields; the total stays in contrastive_frame_count."""
    body = (
        # genuine_alternative: "Unlike X,"
        "CBT-I behaves differently from medication. Unlike sleeping pills, it builds skills. "
        # parasitic: "X is not A. It is B"
        "This is not a quick fix. It is a durable behavioral intervention. "
        # genuine_alternative: "rather than"
        "Clinicians often choose it rather than extended benzodiazepine use."
    )
    scores = scorer.score(body)

    assert scores.contrastive_parasitic_count >= 1
    assert scores.contrastive_alternative_count >= 2
    assert scores.contrastive_frame_count == (
        scores.contrastive_parasitic_count + scores.contrastive_alternative_count
    )


def test_score_parasitic_only_body(scorer):
    """Pure parasitic prose: alternative count stays at 0."""
    body = (
        "Boredom is not a problem to be solved. It is a signal to be heard. "
        "This is not a breakdown. It is a handoff."
    )
    scores = scorer.score(body)

    assert scores.contrastive_parasitic_count == 2
    assert scores.contrastive_alternative_count == 0
    assert scores.contrastive_frame_count == 2


def test_score_counts_em_dashes(scorer):
    """Em dashes (U+2014) are counted; threshold catches overuse."""
    body = (
        "Sleep is layered — REM, deep, light — each doing different work. "
        "Wake someone mid-REM and they will report a vivid dream — "
        "evaporating within minutes. The pattern holds — "
        "across decades of work."
    )
    scores = scorer.score(body)

    # 4 em dashes in this body
    assert scores.em_dash_count == 4
    # At ~30 words, that's >100/1000 — well above default 4.0 threshold
    assert scores.density_per_1000(scores.em_dash_count) > 4.0


def test_score_no_em_dashes_clears_threshold(scorer):
    """A draft without em dashes does not fail on the em dash metric."""
    body = (
        "Sleep is layered. REM, deep, light, each doing different work. "
        "Wake someone mid-REM and they will report a vivid dream that "
        "evaporates within minutes. The pattern holds across decades of work."
    )
    scores = scorer.score(body)

    assert scores.em_dash_count == 0


def test_score_formulaic_transitions_only_at_paragraph_opening(scorer):
    """'Furthermore,' at start of paragraph counts; mid-sentence use does not."""
    body_start = (
        "Furthermore, the evidence shows a clear pattern.\n\n"
        "Additionally, results were consistent across age groups.\n\n"
        "The findings held at six-month follow-up."
    )
    body_midsentence = (
        "The study reported a clear pattern — furthermore findings were robust "
        "and, additionally considered across age groups, held at six-month "
        "follow-up."
    )

    assert scorer.score(body_start).formulaic_transition_count == 2
    assert scorer.score(body_midsentence).formulaic_transition_count == 0
