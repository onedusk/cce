"""Output path configuration data contracts.

A PathConfig defines how a single output path (e.g., 'learn', 'explore', 'apply')
should influence the writer's synthesis behavior.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class PathConfig(BaseModel):
    """Output path definition with writer synthesis overrides."""

    id: str = Field(description="Path identifier (e.g., 'learn')")
    name: str = Field(description="Display name (e.g., 'Learn')")
    description: Optional[str] = Field(
        default=None, description="What this path produces"
    )

    # Synthesis guidance
    tone: Literal["formal", "conversational", "pedagogical", "neutral"] = Field(
        default="neutral",
    )
    structure: Literal["essay", "reference", "actionable"] = Field(
        default="essay",
    )
    depth: Literal["foundational", "contextual", "practical"] = Field(
        default="foundational",
    )
    audience_override: Optional[str] = Field(
        default=None,
        description="Override request.audience for this path (None = use request default)",
    )

    # Content requirements
    section_requirements: list[str] = Field(
        default_factory=list,
        description="Required output sections (e.g., ['overview', 'key_findings'])",
    )
    max_words: Optional[int] = Field(
        default=None,
        description="Soft word count target (None = no limit)",
    )
    prompt_addendum: Optional[str] = Field(
        default=None,
        description="Additional writer instruction appended to system prompt",
    )

    # Per-path tuning (optional — leave None to use pipeline defaults)
    max_evidence: Optional[int] = Field(
        default=None,
        description="Cap evidence objects for this path (None = use all)",
    )
    max_paragraphs: Optional[int] = Field(
        default=None,
        description="Target max substantive paragraphs (None = no limit)",
    )
    subtopic_limit: Optional[int] = Field(
        default=None,
        description="Use only the first N subtopics for this path (None = all)",
    )

    model_config = {"frozen": True}
