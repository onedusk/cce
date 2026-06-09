"""Evidence data contracts.

An Evidence object is a verbatim excerpt from a source, stored with full
provenance so that every downstream claim can be traced back to exactly
where it came from.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SourceQuality(BaseModel):
    """Quality signals captured during discovery, before synthesis."""

    is_peer_reviewed: bool = False
    is_primary_source: bool = False
    domain_reputation: str | None = Field(
        default=None,
        description="Reputation tier from source policy (e.g. 'trusted', 'unknown')",
    )
    conflict_of_interest: bool = Field(
        default=False,
        description="True if source is marketing material, sponsored content, etc.",
    )

    model_config = {"frozen": True}


class Evidence(BaseModel):
    """A single piece of stored evidence with provenance."""

    id: str = Field(description="Unique identifier (generated at extraction time)")
    url: str = Field(description="Canonical URL of the source")
    title: str | None = Field(default=None, description="Page or document title")
    author: str | None = Field(default=None, description="Author if available")
    published_at: datetime | None = Field(
        default=None, description="When the source was originally published"
    )
    retrieved_at: datetime = Field(description="When the engine fetched this source")
    excerpt: str = Field(
        description="Verbatim snippet stored for auditing. Never paraphrased."
    )
    excerpt_hash: str = Field(description="SHA-256 of the excerpt, used for dedup")
    locator: str | None = Field(
        default=None,
        description="Section, heading, or paragraph index within the source",
    )
    source_quality: SourceQuality | None = Field(
        default=None,
        description="Quality metadata assigned during discovery",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Taxonomy tags assigned during discovery (Phase 2)",
    )
    dimension_signals: dict[str, str] = Field(
        default_factory=dict,
        description="Taxonomy dimension_id -> assigned value (Phase 2)",
    )

    model_config = {"frozen": True}


class DiscoveryResult(BaseModel):
    """Return type of Discoverer.discover() — replaces the mutable
    per-instance metrics side-channel deleted in M07 (finding 1.2, ADR-005)."""

    model_config = {"frozen": True}

    evidence: list[Evidence] = Field(default_factory=list)
    metrics: dict[str, int | float] = Field(
        default_factory=dict,
        description=(
            "Keys: crawl_success, crawl_failed, crawl_failure_rate — same "
            "keys previously stashed on the Discoverer instance"
        ),
    )
