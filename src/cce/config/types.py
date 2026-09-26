"""Engine configuration types.

Centralized typed config objects that modules accept as constructor args.
Loaded once by config/loader.py, then distributed -- modules never read
env vars or config files directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import AliasChoices, BaseModel, Field


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
        description=(
            "Lower = more deterministic. Writer and verifier may override. "
            "Not sent to models that reject sampling params (Opus 4.7+, "
            "Sonnet 5) — B1."
        ),
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Max tokens per LLM call, thinking included. None = the model's "
            "maximum output, read from the Anthropic Models API once per "
            "process (128000 on the 4.6 and 5.x models, 64000 on the 4.5 "
            "ones), or 21000 if that lookup fails. Requests are streamed, so "
            "the SDK's ~21,333 non-streaming ceiling no longer applies. On "
            "current models thinking counts against this cap; a reply that "
            "hits it raises IncompleteResponseError."
        ),
    )
    thinking: Literal["adaptive", "disabled"] | None = Field(
        default=None,
        description=(
            "Thinking mode sent as `thinking: {type: ...}` (B2). None = omit "
            "the param and take the model default (Sonnet 5 / Opus 5 think "
            "adaptively; 4.6 models do not think). Never sent to models "
            "without adaptive thinking (Opus 4.5, Haiku 4.5 and older). "
            "Otherwise passed through as set: the API rejects `disabled` on "
            "Fable 5 / Opus 5.5, and on Opus 5 at effort xhigh/max."
        ),
    )
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None,
        description=(
            "Sent as `output_config.effort` (B2). None = omit (model "
            "default, `high` on most models). Sent only to models with "
            "adaptive thinking (4.6 and later) — so also omitted on Opus 4.5, "
            "which does accept effort. `xhigh` needs Opus 4.7+ / Sonnet 5; "
            "the API rejects it on the 4.6 models. Lowering effort is the "
            "lever when thinking crowds out the reply."
        ),
    )


class RoleLLMSettings(BaseModel):
    """Per-role overrides of LLMConfig. None (the default) inherits the
    ``llm`` value; credentials and the provider are always shared. A role
    with any override gets its own provider built from ``llm`` plus these."""

    model: str | None = Field(
        default=None, description="Model for this role. None = LLMConfig.model."
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Per-call output cap for this role. None = LLMConfig.max_tokens.",
    )
    thinking: Literal["adaptive", "disabled"] | None = Field(
        default=None, description="Thinking mode for this role. None = LLMConfig."
    )
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None,
        description=(
            "Effort for this role, e.g. `medium` for the editor so thinking "
            "doesn't crowd out a long rewrite. None = LLMConfig.effort."
        ),
    )


class WriterConfig(RoleLLMSettings):
    """Writer agent call settings. The implied-claim checker uses the
    writer's provider, so these settings apply to it too."""

    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description=(
            "Low for factual consistency; do not increase without testing. "
            "Ignored on models that reject sampling params (B1)."
        ),
    )


class VerifierConfig(RoleLLMSettings):
    """Verifier agent call settings."""

    model: str | None = Field(
        default=None,
        description=(
            "Optional verifier-specific model so the writer and verifier "
            "don't share blind spots (B3). None = the verifier uses "
            "LLMConfig.model. Credentials and the other LLMConfig settings "
            "are inherited, including thinking/effort, so those must also be "
            "valid for this model (e.g. effort xhigh fails on a 4.6 verifier)."
        ),
    )
    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description=(
            "Very low for consistent judgment; do not increase. Ignored on "
            "models that reject sampling params (B1)."
        ),
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-call output cap for the claim-by-claim report (thinking "
            "included). None = LLMConfig.max_tokens (the model's maximum "
            "unless set)."
        ),
    )


class EvidenceStoreConfig(BaseModel):
    """Configuration for the evidence store backend."""

    backend: str = Field(
        default="sqlite", description="Storage backend: sqlite (Phase 1)"
    )
    sqlite_path: Path = Field(
        default=Path("evidence.db"),
        description=(
            "Path to SQLite database file (also holds jobs, packages and API "
            "keys). Process-wide when set via CCE_EVIDENCE_SQLITE_PATH: don't "
            "rely on it for per-tenant separation — inject stores instead (B6)."
        ),
    )


class EmbeddingConfig(BaseModel):
    """Configuration for the embedding provider."""

    enabled: bool = Field(
        default=True,
        description="Enable embedding-based evidence ranking. Falls back to length-based if False or unavailable.",
    )
    provider: str = Field(
        default="ollama",
        description="Embedding provider: ollama (Phase 2)",
    )
    model: str = Field(
        default="nomic-embed-text-v2-moe",
        description="Embedding model identifier",
    )
    dimensions: int = Field(
        default=768,
        description="Embedding vector dimensions (must match model output)",
    )
    base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server URL",
    )
    timeout_seconds: int = Field(
        default=30,
        description="Timeout for embedding API calls",
    )
    batch_size: int = Field(
        default=64,
        ge=1,
        description="Max texts per embedding API call",
    )
    concurrency: int = Field(
        default=1,
        ge=1,
        description=(
            "Max concurrent embedding API calls (audit P2). Default 1 keeps "
            "the behavior sequential until a given backend's concurrency is "
            "verified; raise once you've confirmed the backend handles it."
        ),
    )


class CrawlConfig(BaseModel):
    """Configuration for the crawl adapter."""

    adapter: str = Field(
        default="firecrawl", description="Crawl adapter: firecrawl, crawl4ai"
    )
    api_key: str | None = Field(
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

    pass_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        # Renamed from autopublish_threshold (B8): PASS is a quality signal,
        # and whether it publishes is the publish policy's call. The old key
        # stays accepted, or an operator's YAML threshold would be dropped
        # silently and the profile reset to the default.
        validation_alias=AliasChoices("pass_threshold", "autopublish_threshold"),
        description="Minimum verifier confidence for the gate to PASS",
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


# --- Canonical quality-gate profile templates (audit A3) -------------------
# Single source of truth for the three named risk profiles. Both
# `EngineConfig.quality_gate` and `config/loader._load_gate_config` read from
# here — changing a profile in one place propagates to both.

QUALITY_GATE_PROFILES: Final[dict[str, dict]] = {
    "low": {
        "pass_threshold": 0.7,
        "min_citations_per_paragraph": 1,
        "max_writer_iterations": 2,
    },
    "medium": {
        "pass_threshold": 0.85,
        "min_citations_per_paragraph": 1,
        "max_writer_iterations": 3,
    },
    "high": {
        "pass_threshold": 0.95,
        "min_citations_per_paragraph": 2,
        "max_writer_iterations": 4,
    },
}


def default_quality_gate_profiles() -> dict[str, QualityGateConfig]:
    """Build a fresh {profile_name: QualityGateConfig} dict from the templates."""
    return {
        name: QualityGateConfig(**fields)
        for name, fields in QUALITY_GATE_PROFILES.items()
    }


class APIConfig(BaseModel):
    """API server configuration (Phase 3)."""

    host: str = Field(default="0.0.0.0", description="Bind address")
    port: int = Field(default=8000, description="Bind port")
    require_auth: bool = Field(
        default=True, description="Enable API key authentication"
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        description="CORS allowed origins",
    )
    max_concurrent_jobs: int = Field(
        default=2, ge=1, description="Max simultaneous pipeline runs"
    )


class HumanizationThresholds(BaseModel):
    """Pass/fail thresholds for the programmatic style scorer (H2).

    Calibrated 2026-04-17 against 36 existing MDX drafts (scripts/run_score_sweep.py).
    Defaults reflect the engine's observed distribution on 1000-2000-word
    single-topic essays, not general-prose baselines from the research.
    """

    min_sentence_length_stddev: float = Field(
        default=10.0,
        ge=0.0,
        description=(
            "Below this is 'AI-flat'. Raised from 8.0 (research default) "
            "to the observed p25 on CCE output, where stddev < 10 reliably "
            "indicates genuinely flat prose rather than the natural floor."
        ),
    )
    max_suppressed_vocab_hits_per_1000: float = Field(
        default=3.0,
        ge=0.0,
        description=(
            "Density tolerance for suppressed vocabulary (per 1000 words). "
            "At 3.0 the scorer catches the top quartile of engine output "
            "(p75 density = 3.42); well-calibrated."
        ),
    )
    min_type_token_ratio: float = Field(
        default=0.38,
        ge=0.0,
        le=1.0,
        description=(
            "Lexical diversity floor. Lowered from 0.45 (research default) "
            "to 0.38 (engine p25). Single-topic 1500-2000-word essays reuse "
            "topic-specific vocabulary mechanically — 0.45 was a general-"
            "prose baseline that flagged 86% of CCE drafts as false positives."
        ),
    )
    max_formulaic_transitions_per_1000: float = Field(
        default=2.0,
        ge=0.0,
        description="Density tolerance for 'Furthermore', 'Additionally', etc.",
    )
    max_contrastive_frames_per_1000: float = Field(
        default=5.0,
        ge=0.0,
        description="Density tolerance for contrastive frames. Above triggers H4.",
    )
    max_hedging_density_per_1000: float = Field(
        default=8.0,
        ge=0.0,
        description=(
            "Density tolerance for hedging + stock phrases. Category covers "
            "both ('it should be noted', 'a testament to', 'in today's fast-"
            "paced world', etc.) — both share the same substring-match logic."
        ),
    )
    max_em_dashes_per_1000: float = Field(
        default=4.0,
        ge=0.0,
        description=(
            "Density tolerance for em dash characters (U+2014). The engine's "
            "natural distribution is ~17/1000 (median) on archival output — "
            "well above human-writing norms. 4.0 is an editorial-target "
            "threshold, not a calibrated-to-engine threshold: em dash overuse "
            "is precisely the AI fingerprint we want the editor to address. "
            "Source: Goedecke 2025; Plagiarism Today 2025."
        ),
    )


class EditorConfig(RoleLLMSettings):
    """Editor agent configuration (H3)."""

    enabled: bool = Field(
        default=True,
        description="Independent kill-switch — set False to ship H1+H2 without H3.",
    )
    temperature: float = Field(
        default=0.4,
        ge=0.0,
        le=2.0,
        description="Higher than writer (0.2). Style work benefits from variance.",
    )
    max_words_drift_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Tolerance for word-count drift vs PathConfig.max_words.",
    )


class ImpliedClaimsConfig(BaseModel):
    """Implied-claim checker configuration (H4)."""

    enabled: bool = Field(
        default=True,
        description="Independent kill-switch for H4 (set False to disable).",
    )
    search_strategy: Literal["keyword", "embedding", "llm_extract"] = Field(
        default="llm_extract",
        description=(
            "How to find counter-evidence for a dismissed side. v1: extract "
            "counter-topic via LLM, then call EvidenceStore.search(topic=...). "
            "'embedding' is a future upgrade once Phase-2 vectors are addressable "
            "per-claim."
        ),
    )
    dismissal_release_valve_ratio: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "If counter-evidence <= this fraction of total cited evidence, the "
            "contrast may stand with a brief qualifier rather than a full "
            "spectrum rewrite."
        ),
    )
    counter_evidence_search_limit: int = Field(
        default=10,
        ge=1,
        description="Max evidence items fetched when checking the dismissed side.",
    )


class HumanizationConfig(BaseModel):
    """Master humanization config attached to EngineConfig."""

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch — ON by default (operator preference, 2026-06-24). "
            "When False, scoring/editor/implied-claim stages skip entirely and "
            "the pipeline behaves identically to pre-humanization."
        ),
    )
    markers_path: Path = Field(
        default=Path("config/humanization_markers.yaml"),
        description="Path to the marker-lists YAML (vocab, hedging, transitions, regex).",
    )
    thresholds: HumanizationThresholds = Field(default_factory=HumanizationThresholds)
    editor: EditorConfig = Field(default_factory=EditorConfig)
    implied_claims: ImpliedClaimsConfig = Field(default_factory=ImpliedClaimsConfig)


class EngineConfig(BaseModel):
    """Top-level engine configuration. Constructed by config/loader.py."""

    llm: LLMConfig
    writer: WriterConfig = Field(default_factory=WriterConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    evidence_store: EvidenceStoreConfig = Field(default_factory=EvidenceStoreConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    quality_gate: dict[str, QualityGateConfig] = Field(
        default_factory=default_quality_gate_profiles,
        description="Quality gate thresholds keyed by risk profile name",
    )
    api: APIConfig = Field(default_factory=APIConfig)
    humanization: HumanizationConfig = Field(default_factory=HumanizationConfig)
    publish_policy: Literal["auto", "human"] = Field(
        default="auto",
        description=(
            "What a gate PASS on every path means (B8). 'auto' (default): the "
            "job is COMPLETED, as before. 'human': it is READY_FOR_APPROVAL — "
            "PASS is a quality signal, and a person approves every output. "
            "Process-wide, not per request, so an API client can't downgrade it."
        ),
    )
    max_tokens_per_job: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Hard ceiling on accumulated LLM tokens (input + output, all "
            "paths and iterations) per job. None = unlimited. On breach the "
            "job stops iterating and routes to REVIEW_REQUIRED (ADR-003)."
        ),
    )
    engine_version: str = Field(default="0.1.0")
