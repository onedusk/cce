"""Programmatic style scorer (humanization M02).

Computes :class:`StyleScores` from a content draft. No LLM calls — pure Python
text analysis so the gating decision (whether to invoke the Editor in M03) is
reproducible and unit-testable.

Design decisions (see docs/decompose/humanization/stage-1-design-pack.md):

- ADR-002: programmatic gate, no LLM — avoids per-iteration cost for the
  common case where the writer happened to produce natural prose.
- Citation markers ``[ev:ID]`` are stripped before scoring — they would
  otherwise inflate word count and distort lexical diversity.
- Formulaic transitions are only matched at paragraph openings, not mid-text
  (``"Furthermore,"`` at start of ``\\n\\n``-separated block, not as a word
  inside a sentence).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from statistics import pstdev

from cce.config.markers import HumanizationMarkers
from cce.config.types import HumanizationThresholds
from cce.models.style import StyleScores

logger = logging.getLogger(__name__)


# Canonical citation format is [ev:ID] (colon). Matches the tightened regex
# in cce.synthesis.editor — consistent canonical form across the humanization
# surface. Stripped before scoring so citation markers don't inflate word
# count or distort lexical diversity.
_CITATION_RE = re.compile(r"\[ev:[^\]]+\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")
_WORD_RE = re.compile(r"\b[\w'-]+\b")


class Scorer:
    """Compute StyleScores against configured thresholds and marker lists."""

    def __init__(
        self,
        thresholds: HumanizationThresholds,
        markers: HumanizationMarkers,
    ) -> None:
        self._thresholds = thresholds
        self._markers = markers
        self._contrastive_patterns = markers.compiled_contrastive_patterns()
        self._suppressed_set = {w.lower() for w in markers.suppressed_vocabulary}
        self._hedging_lowered = [h.lower() for h in markers.hedging_phrases]
        self._transitions = list(markers.formulaic_transitions)

    def score(self, content: str) -> StyleScores:
        """Compute StyleScores for a draft. Citation markers are stripped first."""
        body = _CITATION_RE.sub("", content).strip()
        if not body:
            return _empty_scores()

        sentences = _split_sentences(body)
        words = _WORD_RE.findall(body)
        word_count = len(words)

        sentence_lengths = [len(_WORD_RE.findall(s)) for s in sentences if s.strip()]
        stddev = pstdev(sentence_lengths) if len(sentence_lengths) > 1 else 0.0

        suppressed_hits = self._count_suppressed(words)
        ttr = _type_token_ratio(words)
        transition_count = self._count_transitions(body)
        contrastive_count = self._count_contrastive(body)
        hedging_count = self._count_hedging(body.lower())

        per_1000 = (1000.0 / word_count) if word_count else 0.0
        passes = (
            stddev >= self._thresholds.min_sentence_length_stddev
            and (suppressed_hits * per_1000)
            <= self._thresholds.max_suppressed_vocab_hits_per_1000
            and ttr >= self._thresholds.min_type_token_ratio
            and (transition_count * per_1000)
            <= self._thresholds.max_formulaic_transitions_per_1000
            and (contrastive_count * per_1000)
            <= self._thresholds.max_contrastive_frames_per_1000
            and (hedging_count * per_1000)
            <= self._thresholds.max_hedging_density_per_1000
        )

        return StyleScores(
            sentence_length_stddev=round(stddev, 3),
            suppressed_vocab_hits=suppressed_hits,
            type_token_ratio=round(ttr, 3),
            formulaic_transition_count=transition_count,
            contrastive_frame_count=contrastive_count,
            hedging_phrase_count=hedging_count,
            word_count=word_count,
            humanization_pass=passes,
        )

    def _count_suppressed(self, words: Iterable[str]) -> int:
        return sum(1 for w in words if w.lower() in self._suppressed_set)

    def _count_transitions(self, body: str) -> int:
        """Only count transitions that open a paragraph — mid-sentence uses are fine."""
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        return sum(1 for p in paragraphs for t in self._transitions if p.startswith(t))

    def _count_contrastive(self, body: str) -> int:
        return sum(len(p.findall(body)) for p in self._contrastive_patterns)

    def _count_hedging(self, body_lower: str) -> int:
        return sum(body_lower.count(h) for h in self._hedging_lowered)


def _split_sentences(body: str) -> list[str]:
    """Naive sentence split.

    Splits on terminal punctuation followed by whitespace then a capital letter
    or opening quote. Good enough for engine-sized drafts; avoids pulling in
    nltk for marginal accuracy gains on prose that's already reasonably clean.
    """
    return _SENTENCE_SPLIT_RE.split(body)


def _type_token_ratio(words: list[str]) -> float:
    if not words:
        return 0.0
    lowered = [w.lower() for w in words]
    return len(set(lowered)) / len(lowered)


def _empty_scores() -> StyleScores:
    """Empty content can't fail style — verifier owns emptiness."""
    return StyleScores(
        sentence_length_stddev=0.0,
        suppressed_vocab_hits=0,
        type_token_ratio=0.0,
        formulaic_transition_count=0,
        contrastive_frame_count=0,
        hedging_phrase_count=0,
        word_count=0,
        humanization_pass=True,
    )
