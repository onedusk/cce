"""Curation request data contracts.

A CurationRequest is the only required input to run the engine.
"""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from cce.models.evidence import Evidence

# An ID has to fit inside a [ev:ID] marker (parsing.EV_MARKER_RE), and the
# gate reads a "," inside one as several IDs.
_CITABLE_ID_RE = re.compile(r"[^\s\[\],]+")


class CurationConstraints(BaseModel):
    """Filters applied to source discovery."""

    date_from: str | None = Field(
        default=None, description="ISO date string, lower bound for source recency"
    )
    date_to: str | None = Field(
        default=None, description="ISO date string, upper bound for source recency"
    )
    domains_allow: list[str] = Field(
        default_factory=list, description="Only include sources from these domains"
    )
    domains_deny: list[str] = Field(
        default_factory=list, description="Exclude sources from these domains"
    )
    jurisdiction: str | None = Field(
        default=None, description="Legal/regulatory jurisdiction filter"
    )

    model_config = {"frozen": True}


class CurationRequest(BaseModel):
    """Input contract for a curation run."""

    MAX_SUBTOPIC_LENGTH: ClassVar[int] = 200

    topic: str = Field(
        ..., min_length=1, max_length=500, description="Primary topic to curate"
    )
    subtopics: list[str] = Field(
        default_factory=list, max_length=20, description="Optional subtopics to cover"
    )
    paths: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Output paths to generate, drawn from registered PathConfig",
    )
    audience: str = Field(
        default="general",
        max_length=100,
        description="Target audience (free-form or enum per product)",
    )
    constraints: CurationConstraints | None = Field(
        default=None, description="Discovery filters"
    )
    policy_id: str = Field(
        ..., min_length=1, description="Which SourcePolicy config to use"
    )
    taxonomy_id: str | None = Field(
        default=None,
        description="Which TaxonomyConfig to use (Phase 2, optional for Phase 1)",
    )
    path_config_id: str | None = Field(
        default=None,
        description="Which PathConfig to use (Phase 2, optional for Phase 1)",
    )
    risk_profile: str = Field(
        default="medium",
        pattern=r"^(low|medium|high)$",
        description="Maps to quality gate thresholds: low, medium, high",
    )
    context: list[Evidence] = Field(
        default_factory=list,
        description=(
            "Pinned evidence (B11): settled statements the caller already "
            "knows. Skips discovery, the 50-character fragment minimum and "
            "every cap; shown to the writer and verifier as settled context, "
            "citable as [ev:ID] and stored under the caller's IDs. IDs must "
            "be unique and contain no whitespace, '[', ']' or ','; "
            "excerpt_hash must be the SHA-256 hex of the excerpt."
        ),
    )

    @field_validator("paths")
    @classmethod
    def _paths_unique(cls, v: list[str]) -> list[str]:
        # Everything downstream is keyed by path; a repeat would leave one
        # draft unrecorded (review of B7). Dropped, order kept.
        return list(dict.fromkeys(v))

    @field_validator("context")
    @classmethod
    def _context_citable(cls, v: list[Evidence]) -> list[Evidence]:
        seen: set[str] = set()
        for ev in v:
            if not _CITABLE_ID_RE.fullmatch(ev.id):
                raise ValueError(
                    f"context id {ev.id[:50]!r} can't be cited as [ev:ID]: no "
                    "whitespace, '[', ']' or ','"
                )
            if ev.id in seen:
                raise ValueError(f"duplicate context id {ev.id!r}")
            seen.add(ev.id)
            if ev.excerpt_hash != hashlib.sha256(ev.excerpt.encode()).hexdigest():
                raise ValueError(
                    f"context {ev.id!r}: excerpt_hash is not the SHA-256 of excerpt"
                )
        return v

    @field_validator("subtopics")
    @classmethod
    def _subtopic_elements_bounded(cls, v: list[str]) -> list[str]:
        for s in v:
            if len(s) > cls.MAX_SUBTOPIC_LENGTH:
                raise ValueError(
                    f"subtopic exceeds {cls.MAX_SUBTOPIC_LENGTH} chars: {s[:50]}…"
                )
        return v

    model_config = {"frozen": True}
