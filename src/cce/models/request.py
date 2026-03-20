"""Curation request data contracts.

A CurationRequest is the only required input to run the engine.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CurationConstraints(BaseModel):
    """Filters applied to source discovery."""

    date_from: Optional[str] = Field(
        default=None, description="ISO date string, lower bound for source recency"
    )
    date_to: Optional[str] = Field(
        default=None, description="ISO date string, upper bound for source recency"
    )
    domains_allow: list[str] = Field(
        default_factory=list, description="Only include sources from these domains"
    )
    domains_deny: list[str] = Field(
        default_factory=list, description="Exclude sources from these domains"
    )
    jurisdiction: Optional[str] = Field(
        default=None, description="Legal/regulatory jurisdiction filter"
    )


class CurationRequest(BaseModel):
    """Input contract for a curation run."""

    topic: str = Field(description="Primary topic to curate")
    subtopics: list[str] = Field(
        default_factory=list, description="Optional subtopics to cover"
    )
    paths: list[str] = Field(
        description="Output paths to generate, drawn from registered PathConfig"
    )
    audience: str = Field(
        default="general",
        description="Target audience (free-form or enum per product)",
    )
    constraints: Optional[CurationConstraints] = Field(
        default=None, description="Discovery filters"
    )
    policy_id: str = Field(description="Which SourcePolicy config to use")
    taxonomy_id: Optional[str] = Field(
        default=None,
        description="Which TaxonomyConfig to use (Phase 2, optional for Phase 1)",
    )
    path_config_id: Optional[str] = Field(
        default=None,
        description="Which PathConfig to use (Phase 2, optional for Phase 1)",
    )
    risk_profile: str = Field(
        default="medium",
        description="Maps to quality gate thresholds: low, medium, high",
    )
