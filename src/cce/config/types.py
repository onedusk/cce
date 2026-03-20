"""Engine configuration types.

Centralized typed config objects that modules accept as constructor args.
Loaded once by config/loader.py, then distributed -- modules never read
env vars or config files directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """Configuration for the LLM provider."""

    provider: str = Field(
        default="anthropic", description="LLM provider: anthropic, openai"
    )
    model: str = Field(
        default="claude-sonnet-4-6",
        description="Model identifier",
    )
    api_key: str = Field(description="API key (loaded from env var)")
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Lower = more deterministic. Writer and verifier may override.",
    )
    max_tokens: int = Field(default=4096, description="Max tokens per LLM call")


class EvidenceStoreConfig(BaseModel):
    """Configuration for the evidence store backend."""

    backend: str = Field(
        default="sqlite", description="Storage backend: sqlite (Phase 1)"
    )
    sqlite_path: Path = Field(
        default=Path("evidence.db"),
        description="Path to SQLite database file",
    )


class CrawlConfig(BaseModel):
    """Configuration for the crawl adapter."""

    adapter: str = Field(
        default="firecrawl", description="Crawl adapter: firecrawl, crawl4ai"
    )
    api_key: Optional[str] = Field(
        default=None, description="API key if required by the adapter"
    )
    rate_limit_rps: float = Field(
        default=2.0, description="Max requests per second to crawl sources"
    )
    timeout_seconds: int = Field(default=30, description="Per-page crawl timeout")
    max_excerpts_per_source: int = Field(
        default=5,
        description="Max evidence excerpts to keep per source URL (longest preferred)",
    )
    max_evidence_total: int = Field(
        default=100,
        description="Global cap on total evidence objects after per-source filtering",
    )


class QualityGateConfig(BaseModel):
    """Threshold configuration for the quality gate, keyed by risk profile."""

    autopublish_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score to autopublish",
    )
    min_citations_per_paragraph: int = Field(
        default=1, description="Minimum citations required per paragraph"
    )
    min_citation_density_ratio: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Minimum ratio of substantive paragraphs that must meet citation threshold",
    )
    max_writer_iterations: int = Field(
        default=3,
        description="Max writer-verifier loop iterations before routing to review",
    )


class EngineConfig(BaseModel):
    """Top-level engine configuration. Constructed by config/loader.py."""

    llm: LLMConfig
    evidence_store: EvidenceStoreConfig = Field(default_factory=EvidenceStoreConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    quality_gate: dict[str, QualityGateConfig] = Field(
        default_factory=lambda: {
            "low": QualityGateConfig(
                autopublish_threshold=0.7,
                min_citations_per_paragraph=1,
                max_writer_iterations=2,
            ),
            "medium": QualityGateConfig(
                autopublish_threshold=0.85,
                min_citations_per_paragraph=1,
                max_writer_iterations=3,
            ),
            "high": QualityGateConfig(
                autopublish_threshold=0.95,
                min_citations_per_paragraph=2,
                max_writer_iterations=4,
            ),
        },
        description="Quality gate thresholds keyed by risk profile name",
    )
    engine_version: str = Field(default="0.1.0")
