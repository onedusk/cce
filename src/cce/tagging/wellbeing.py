"""Rules-based well-being taxonomy classifier.

Reference implementation of TaxonomyPlugin for Thnk Labs' 8 Dimensions
of Well-Being. Uses keyword and domain heuristics — no LLM calls.
"""

from __future__ import annotations

import re
from typing import ClassVar

from cce.models.evidence import Evidence
from cce.models.taxonomy import TaxonomyConfig
from cce.tagging.base import TaggingResult

# Dimension keyword patterns: dimension_id -> list of regex patterns
_DIMENSION_PATTERNS: dict[str, list[str]] = {
    "intellectual": [
        r"\bcogniti\w+\b",
        r"\bthink\w+\b",
        r"\battention\b",
        r"\bfocus\b",
        r"\bmemory\b",
        r"\blearn\w+\b",
        r"\bdecision\b",
        r"\breason\w+\b",
    ],
    "emotional": [
        r"\banxie\w+\b",
        r"\bstress\b",
        r"\bmood\b",
        r"\bemotion\w+\b",
        r"\birritab\w+\b",
        r"\bresilien\w+\b",
        r"\bwellbeing\b",
        r"\bwell-being\b",
    ],
    "physical": [
        r"\bsleep\b",
        r"\bexercis\w+\b",
        r"\bbody\b",
        r"\bphysical\b",
        r"\bhealth\b",
        r"\bmovemen\w+\b",
        r"\bfatigu\w+\b",
        r"\bpain\b",
    ],
    "environmental": [
        r"\benvironmen\w+\b",
        r"\bnoise\b",
        r"\blight\w*\b",
        r"\bclutter\b",
        r"\bspace\b",
        r"\bdesign\b",
        r"\barchitect\w+\b",
        r"\btemperatur\w+\b",
    ],
    "financial": [
        r"\bfinanc\w+\b",
        r"\bmoney\b",
        r"\bbudget\w+\b",
        r"\bdebt\b",
        r"\bincome\b",
        r"\bsaving\w+\b",
        r"\binvest\w+\b",
        r"\beconom\w+\b",
    ],
    "social": [
        r"\bsocial\b",
        r"\brelationship\w*\b",
        r"\bloneli\w+\b",
        r"\bcommunit\w+\b",
        r"\bbelonging\b",
        r"\bisolat\w+\b",
        r"\bfriend\w+\b",
        r"\bfamil\w+\b",
    ],
    "spiritual": [
        r"\bspiritual\w*\b",
        r"\bmeaning\b",
        r"\bpurpose\b",
        r"\bgratitud\w+\b",
        r"\bmindful\w+\b",
        r"\bmeditat\w+\b",
        r"\bvalu\w+\b",
        r"\bfaith\b",
    ],
    "vocational": [
        r"\bwork\b",
        r"\bcareer\b",
        r"\bjob\b",
        r"\bprofession\w+\b",
        r"\bproductiv\w+\b",
        r"\bburnout\b",
        r"\bvocation\w+\b",
        r"\bemploy\w+\b",
    ],
}

# Single alternation pattern per dimension (8 patterns, not 64 — 8x fewer engine calls)
_COMPILED_PATTERNS: dict[str, re.Pattern] = {
    dim_id: re.compile("|".join(patterns), re.IGNORECASE)
    for dim_id, patterns in _DIMENSION_PATTERNS.items()
}


class WellBeingTaxonomy:
    """Rules-based classifier for 8 Dimensions of Well-Being.

    Scores each evidence excerpt against keyword patterns for each dimension.
    Assigns 'primary' to the highest-scoring dimension, 'secondary' to others
    above a threshold, 'none' to the rest.
    """

    PRIMARY_THRESHOLD: ClassVar[int] = 3
    SECONDARY_THRESHOLD: ClassVar[int] = 1

    def __init__(self, config: TaxonomyConfig) -> None:
        self._config = config

    async def tag(self, evidence: Evidence) -> TaggingResult:
        """Tag evidence against well-being dimensions."""
        text = f"{evidence.title or ''} {evidence.excerpt}".lower()
        scores = self._score_dimensions(text)

        if not scores:
            return TaggingResult(tags=[], signals={}, confidence=0.0)

        max_score = max(scores.values())
        signals: dict[str, str] = {}
        tags: list[str] = []

        for dim_id in self._config.dimension_ids():
            score = scores.get(dim_id, 0)
            if score >= self.PRIMARY_THRESHOLD and score == max_score:
                signals[dim_id] = "primary"
                tags.append(dim_id)
            elif score >= self.SECONDARY_THRESHOLD:
                signals[dim_id] = "secondary"
                tags.append(dim_id)
            else:
                signals[dim_id] = "none"

        total_matches = sum(scores.values())
        confidence = min(1.0, total_matches / 10.0)

        return TaggingResult(tags=tags, signals=signals, confidence=confidence)

    async def tag_many(self, evidence: list[Evidence]) -> list[TaggingResult]:
        """Tag multiple evidence objects (sequential, no batching needed)."""
        return [await self.tag(ev) for ev in evidence]

    @staticmethod
    def _score_dimensions(text: str) -> dict[str, int]:
        """Count keyword matches per dimension (one regex pass per dimension)."""
        scores: dict[str, int] = {}
        for dim_id, pattern in _COMPILED_PATTERNS.items():
            count = len(pattern.findall(text))
            if count > 0:
                scores[dim_id] = count
        return scores
