"""Style scoring data contracts.

`StyleScores` is the output of the programmatic scorer (synthesis/scoring.py).
It carries measurable AI-fingerprint metrics for a single ContentUnit. Used to
gate Editor invocation (H3) and logged into StageRecord.metrics for threshold
calibration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StyleScores(BaseModel):
    """Programmatic style metrics for a single content draft.

    All counts are absolute. ``humanization_pass`` is precomputed against the
    active HumanizationThresholds so gate consumers don't need to re-evaluate
    threshold logic.
    """

    sentence_length_stddev: float = Field(
        ge=0.0,
        description="Std dev of sentence lengths in words. Higher = more bursty/human.",
    )
    suppressed_vocab_hits: int = Field(
        ge=0,
        description="Count of tokens matching the suppressed-vocabulary list.",
    )
    type_token_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="Lexical diversity (unique tokens / total tokens).",
    )
    formulaic_transition_count: int = Field(
        ge=0,
        description="Count of paragraph openings matching formulaic-transition list.",
    )
    contrastive_frame_count: int = Field(
        ge=0,
        description=(
            "Total count of contrastive-frame regex matches (both subtypes). "
            "Equals contrastive_parasitic_count + contrastive_alternative_count; "
            "kept as a real field for backward compatibility with pre-0.2.0 "
            "consumers and for the existing humanization threshold gate."
        ),
    )
    contrastive_parasitic_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Subtype count: parasitic contrasts — Y is a degraded/reframed form "
            "of A rather than an independent alternative. Editor collapses "
            "these; ImpliedClaimChecker skips the LLM topic-extraction call."
        ),
    )
    contrastive_alternative_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Subtype count: genuine-alternative contrasts — Y is a real "
            "alternative to X with its own evidence potential. Editor applies "
            "the spectrum principle when ImpliedClaimChecker annotates with "
            "counter-evidence; otherwise the frame is fair game for the "
            "strategy menu."
        ),
    )
    hedging_phrase_count: int = Field(
        ge=0,
        description="Count of hedging-phrase matches in the body.",
    )
    em_dash_count: int = Field(
        ge=0,
        description=(
            "Count of em dash characters (U+2014) in the body. AI writers — "
            "especially GPT-4o, but also Claude — overuse em dashes vs. typical "
            "human prose. Source: Goedecke 2025; Plagiarism Today 2025."
        ),
    )
    word_count: int = Field(
        ge=0,
        description="Total body word count (citations stripped). Density denominator.",
    )
    humanization_pass: bool = Field(
        description="True iff every metric meets its configured threshold.",
    )

    model_config = {"frozen": True}

    def density_per_1000(self, count: int) -> float:
        """Compute per-1000-word density. Zero-safe for empty content."""
        if self.word_count == 0:
            return 0.0
        return (count / self.word_count) * 1000.0
