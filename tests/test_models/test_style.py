"""Tests for the StyleScores model (M02)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cce.models.style import StyleScores

pytestmark = pytest.mark.unit


def _valid_scores(**overrides) -> StyleScores:
    defaults = dict(
        sentence_length_stddev=9.0,
        suppressed_vocab_hits=0,
        type_token_ratio=0.55,
        formulaic_transition_count=0,
        contrastive_frame_count=0,
        hedging_phrase_count=0,
        em_dash_count=0,
        word_count=1000,
        humanization_pass=True,
    )
    defaults.update(overrides)
    return StyleScores(**defaults)


def test_style_scores_constructs_with_required_fields():
    scores = _valid_scores()
    assert scores.sentence_length_stddev == 9.0
    assert scores.humanization_pass is True


def test_style_scores_rejects_negative_counts():
    with pytest.raises(ValidationError):
        _valid_scores(suppressed_vocab_hits=-1)


def test_style_scores_rejects_ttr_above_one():
    with pytest.raises(ValidationError):
        _valid_scores(type_token_ratio=1.5)


def test_style_scores_is_frozen():
    scores = _valid_scores()
    with pytest.raises(ValidationError):
        scores.humanization_pass = False  # type: ignore[misc]


def test_style_scores_round_trips_json():
    original = _valid_scores(contrastive_frame_count=2, hedging_phrase_count=5)
    rehydrated = StyleScores.model_validate_json(original.model_dump_json())
    assert rehydrated == original


def test_density_per_1000_handles_zero_word_count():
    scores = _valid_scores(word_count=0, humanization_pass=True)
    assert scores.density_per_1000(5) == 0.0


def test_density_per_1000_computes_correctly():
    scores = _valid_scores(word_count=500)
    # 2 hits in 500 words = 4 per 1000
    assert scores.density_per_1000(2) == 4.0
