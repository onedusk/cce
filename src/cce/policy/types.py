"""Source policy data contracts.

A SourcePolicy defines the rules for what sources the engine will accept.
It feeds both Discovery (what to crawl) and Verification (what to trust).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ReputationRule(BaseModel):
    """Defines how to score source reputation."""

    require_peer_reviewed: bool = Field(
        default=False, description="Only accept peer-reviewed sources"
    )
    require_primary_source: bool = Field(
        default=False, description="Only accept primary sources (not aggregators)"
    )
    trusted_institutions: list[str] = Field(
        default_factory=list,
        description="Domains or org names treated as trusted (.edu, .gov, etc.)",
    )
    block_marketing: bool = Field(
        default=True,
        description="Filter out pages that look like marketing/sponsored content",
    )


class RecencyRule(BaseModel):
    """Controls how source freshness affects acceptance."""

    max_age_days: Optional[int] = Field(
        default=None,
        description="Reject sources older than this many days. None = no limit.",
    )
    prefer_recent: bool = Field(
        default=True,
        description="When true, discovery prioritizes recent sources in ranking",
    )


class TopicOverride(BaseModel):
    """Per-topic policy adjustments that layer on top of the base policy."""

    topic_pattern: str = Field(
        description="Regex or keyword pattern to match against the request topic"
    )
    domains_allow: list[str] = Field(default_factory=list)
    domains_deny: list[str] = Field(default_factory=list)
    reputation: Optional[ReputationRule] = None
    recency: Optional[RecencyRule] = None


class SourcePolicy(BaseModel):
    """Complete source policy for a curation run."""

    id: str = Field(description="Policy identifier, referenced by CurationRequest")
    name: str = Field(description="Human-readable name")
    domains_allow: list[str] = Field(
        default_factory=list,
        description="Global domain allowlist. Empty = allow all (minus deny list).",
    )
    domains_deny: list[str] = Field(
        default_factory=list,
        description="Global domain denylist. Applied after allowlist.",
    )
    reputation: ReputationRule = Field(default_factory=ReputationRule)
    recency: RecencyRule = Field(default_factory=RecencyRule)
    max_sources_per_run: int = Field(
        default=50,
        description="Cap on total sources discovered per curation run",
    )
    topic_overrides: list[TopicOverride] = Field(
        default_factory=list,
        description="Per-topic policy adjustments",
    )
