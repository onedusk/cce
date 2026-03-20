"""Evidence data contracts.

An Evidence object is a verbatim excerpt from a source, stored with full
provenance so that every downstream claim can be traced back to exactly
where it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SourceQuality(BaseModel):
    """Quality signals captured during discovery, before synthesis."""

    is_peer_reviewed: bool = False
    is_primary_source: bool = False
    domain_reputation: Optional[str] = Field(
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
    title: Optional[str] = Field(default=None, description="Page or document title")
    author: Optional[str] = Field(default=None, description="Author if available")
    published_at: Optional[datetime] = Field(
        default=None, description="When the source was originally published"
    )
    retrieved_at: datetime = Field(description="When the engine fetched this source")
    excerpt: str = Field(
        description="Verbatim snippet stored for auditing. Never paraphrased."
    )
    excerpt_hash: str = Field(description="SHA-256 of the excerpt, used for dedup")
    locator: Optional[str] = Field(
        default=None,
        description="Section, heading, or paragraph index within the source",
    )
    source_quality: Optional[SourceQuality] = Field(
        default=None,
        description="Quality metadata assigned during discovery",
    )

    model_config = {"frozen": True}
