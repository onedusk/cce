"""Curation request data contracts.

A CurationRequest is the only required input to run the engine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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

    model_config = {"frozen": True}
