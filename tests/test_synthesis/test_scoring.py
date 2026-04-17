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
    body = (
        "Sleep breaks down into stages. Each one does different work — some repair "
        "the body, some consolidate memory, some don't seem to do much we can name. "
        "Wake someone mid-REM and they'll report vivid dreams.\n\n"
        "CBT-I targets the habits that keep people awake. It works. Six to eight "
        "sessions, usually, with homework between. The pattern shows up in the "
        "research: people who finish the course sleep better a year later, two years "
        "later, five years later, which is more than you can say for most sleep "
        "medications. Medication can still help during an acute episode, but it's "
        "not a long-term strategy on its own.\n\n"
        "Why does this work? Because the habits are load-bearing. You fix the habits, "
        "the sleep follows. Short feedback loop, measurable outcome."
    )

    scores = scorer.score(body)

    assert scores.word_count > 100
    assert scores.sentence_length_stddev >= 8.0
    assert scores.type_token_ratio >= 0.45
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
