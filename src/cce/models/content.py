"""Content unit data contracts.

A ContentUnit is the engine's output for a single output path -- a piece of
synthesized content with inline citations and an evidence map that ties
every claim back to stored evidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cce.models.style import StyleScores


class Citation(BaseModel):
    """A reference from content to a stored evidence object."""

    evidence_id: str = Field(description="ID of the Evidence object")
    url: str = Field(description="Source URL (denormalized for convenience)")

    model_config = {"frozen": True}


class ClaimMapping(BaseModel):
    """Maps a single claim in the content to its supporting evidence."""

    claim: str = Field(description="The claim text as it appears in the content")
    evidence_ids: list[str] = Field(
        description="IDs of Evidence objects that support this claim"
    )

    model_config = {"frozen": True}


class ContentScores(BaseModel):
    """Quality scores assigned by the verifier."""

    confidence: float = Field(
        ge=0.0, le=1.0, description="Overall confidence in the content"
    )
    coverage: float = Field(ge=0.0, le=1.0, description="How well the topic is covered")
    source_diversity: float = Field(
        ge=0.0, le=1.0, description="Variety of independent sources used"
    )

    model_config = {"frozen": True}


class ContentLineage(BaseModel):
    """Provenance metadata for a content unit."""

    policy_id: str
    taxonomy_id: str = ""
    path_config_id: str = ""
    run_id: str
    engine_version: str

    model_config = {"frozen": True}


class ContentUnit(BaseModel):
    """A single piece of curated content for one output path."""

    id: str = Field(description="Unique identifier")
    path: str = Field(description="Which output path this content belongs to")
    tags: list[str] = Field(
        default_factory=list,
        description="Taxonomy tags (populated in Phase 2)",
    )
    content: str = Field(description="Rendered content (markdown/mdx/html)")
    citations: list[Citation] = Field(
        default_factory=list, description="All citations used in this content"
    )
    evidence_map: list[ClaimMapping] = Field(
        default_factory=list,
        description="Claim-to-evidence mapping for auditability",
    )
    scores: ContentScores
    style_scores: StyleScores | None = Field(
        default=None,
        description="Programmatic style metrics from H2 scorer. None if humanization disabled.",
    )
    draft_source: Literal["writer", "editor"] = Field(
        default="writer",
        description=(
            "Which agent produced the final draft. 'editor' only when the "
            "humanization editor succeeded AND citation preservation held; "
            "stays 'writer' on fallback (finding 1.5)."
        ),
    )
    lineage: ContentLineage

    model_config = {"frozen": True}

    def with_scores(self, scores: ContentScores) -> ContentUnit:
        """Frozen-model update helper — replaces the 9-field
        model_copy(update=...) previously inlined in the pipeline."""
        return self.model_copy(update={"scores": scores})

    def with_style_scores(self, style_scores: StyleScores) -> ContentUnit:
        return self.model_copy(update={"style_scores": style_scores})
