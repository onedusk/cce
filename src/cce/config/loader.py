"""Configuration loader.

Single entry point for loading engine configuration. Reads from environment
variables and optionally from a YAML file. Environment variables take
precedence over YAML values.

Usage:
    config = load_config()                    # env vars only
    config = load_config("config.yaml")       # YAML + env var overrides
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from cce.config.types import (
    APIConfig,
    CrawlConfig,
    EditorConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    HumanizationThresholds,
    ImpliedClaimsConfig,
    LLMConfig,
    QualityGateConfig,
    default_quality_gate_profiles,
)


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid.

    Caught at CLI/app entry points and rendered as a one-line message
    (ADR-006). Never raised by load_config() itself — keyless commands
    (emit-mdx, api key generate) must keep working.
    """


def validate_required_keys(config: EngineConfig, *, require_crawl: bool = True) -> None:
    """Fail fast with the exact env-var name (finding 4.3).

    Called by CurationEngine.embedded(), the API lifespan, and
    pipeline-running CLI commands — NOT by load_config().
    """
    if not config.llm.api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Add it to .env or the environment "
            "(see .env.example)."
        )
    if require_crawl and not config.crawl.api_key:
        raise ConfigError(
            "FIRECRAWL_API_KEY is not set. Add it to .env or the environment "
            "(see .env.example)."
        )


def _coerce_bool(value: str | bool) -> bool:
    """Coerce a string or bool to bool. Handles env var strings like 'false', '0', 'no'."""
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no")
    return bool(value)


def load_config(config_path: str | Path | None = None) -> EngineConfig:
    """Load engine configuration from YAML file + environment variables.

    Environment variables take precedence over YAML values.

    Env var mapping:
        CCE_LLM_PROVIDER        -> llm.provider
        CCE_LLM_MODEL           -> llm.model
        CCE_LLM_API_KEY         -> llm.api_key
        CCE_LLM_TEMPERATURE     -> llm.temperature
        CCE_LLM_MAX_TOKENS      -> llm.max_tokens
        CCE_EVIDENCE_BACKEND    -> evidence_store.backend
        CCE_EVIDENCE_SQLITE_PATH -> evidence_store.sqlite_path
        CCE_CRAWL_ADAPTER       -> crawl.adapter
        CCE_CRAWL_API_KEY       -> crawl.api_key
        CCE_CRAWL_RATE_LIMIT    -> crawl.rate_limit_rps
        CCE_CRAWL_TIMEOUT       -> crawl.timeout_seconds
    """
    file_data: dict[str, Any] = {}
    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                file_data = yaml.safe_load(f) or {}

    return EngineConfig(
        llm=_load_llm_config(file_data.get("llm", {})),
        evidence_store=_load_evidence_config(file_data.get("evidence_store", {})),
        crawl=_load_crawl_config(file_data.get("crawl", {})),
        embedding=_load_embedding_config(file_data.get("embedding", {})),
        quality_gate=_load_gate_config(file_data.get("quality_gate", {})),
        api=_load_api_config(file_data.get("api", {})),
        humanization=_load_humanization_config(file_data.get("humanization", {})),
        engine_version=file_data.get("engine_version", "0.1.0"),
    )


def _load_llm_config(file: dict) -> LLMConfig:
    return LLMConfig(
        provider=os.getenv("CCE_LLM_PROVIDER", file.get("provider", "anthropic")),
        model=os.getenv("CCE_LLM_MODEL")
        or os.getenv("ANTHROPIC_MODEL")
        or file.get("model", "claude-sonnet-4-6"),
        api_key=os.getenv("CCE_LLM_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or file.get("api_key", ""),
        temperature=float(
            os.getenv("CCE_LLM_TEMPERATURE", file.get("temperature", 0.2))
        ),
        max_tokens=int(os.getenv("CCE_LLM_MAX_TOKENS", file.get("max_tokens", 4096))),
    )


def _load_evidence_config(file: dict) -> EvidenceStoreConfig:
    return EvidenceStoreConfig(
        backend=os.getenv("CCE_EVIDENCE_BACKEND", file.get("backend", "sqlite")),
        sqlite_path=Path(
            os.getenv(
                "CCE_EVIDENCE_SQLITE_PATH",
                file.get("sqlite_path", "evidence.db"),
            )
        ),
    )


def _load_crawl_config(file: dict) -> CrawlConfig:
    return CrawlConfig(
        adapter=os.getenv("CCE_CRAWL_ADAPTER", file.get("adapter", "firecrawl")),
        api_key=os.getenv("CCE_CRAWL_API_KEY")
        or os.getenv("FIRECRAWL_API_KEY")
        or file.get("api_key"),
        rate_limit_rps=float(
            os.getenv("CCE_CRAWL_RATE_LIMIT", file.get("rate_limit_rps", 2.0))
        ),
        timeout_seconds=int(
            os.getenv("CCE_CRAWL_TIMEOUT", file.get("timeout_seconds", 30))
        ),
        max_excerpts_per_source=int(
            os.getenv(
                "CCE_CRAWL_MAX_PER_SOURCE", file.get("max_excerpts_per_source", 5)
            )
        ),
        max_evidence_total=int(
            os.getenv("CCE_CRAWL_MAX_EVIDENCE", file.get("max_evidence_total", 100))
        ),
    )


def _load_embedding_config(file: dict) -> EmbeddingConfig:
    enabled_raw = os.getenv("CCE_EMBEDDING_ENABLED", file.get("enabled", True))
    return EmbeddingConfig(
        enabled=_coerce_bool(enabled_raw),
        provider=os.getenv("CCE_EMBEDDING_PROVIDER", file.get("provider", "ollama")),
        model=os.getenv(
            "CCE_EMBEDDING_MODEL", file.get("model", "nomic-embed-text-v2-moe")
        ),
        dimensions=int(
            os.getenv("CCE_EMBEDDING_DIMENSIONS", file.get("dimensions", 768))
        ),
        base_url=os.getenv(
            "CCE_EMBEDDING_BASE_URL", file.get("base_url", "http://localhost:11434")
        ),
        timeout_seconds=int(
            os.getenv("CCE_EMBEDDING_TIMEOUT", file.get("timeout_seconds", 30))
        ),
        batch_size=int(
            os.getenv("CCE_EMBEDDING_BATCH_SIZE", file.get("batch_size", 64))
        ),
    )


def _load_gate_config(file: dict) -> dict[str, QualityGateConfig]:
    """Load quality gate configs. Profile templates come from the canonical
    `QUALITY_GATE_PROFILES` dict in `config/types.py` (audit A3); YAML entries
    override them per-profile.
    """
    result = default_quality_gate_profiles()
    if not file:
        return result

    for profile_name, profile_data in file.items():
        if isinstance(profile_data, dict):
            result[profile_name] = QualityGateConfig(**profile_data)
    return result


def _load_humanization_config(file: dict) -> HumanizationConfig:
    """Load HumanizationConfig from YAML, with env-var overlay for the coarse knobs.

    Granular threshold tuning belongs in YAML where reviewers can see it diffed;
    env vars are provided only for the master switch and the marker file path so
    operators can flip humanization on/off without editing YAML.
    """
    enabled_raw = os.getenv("CCE_HUMANIZATION_ENABLED", file.get("enabled", False))

    thresholds_data = file.get("thresholds", {}) or {}
    editor_data = file.get("editor", {}) or {}
    implied_data = file.get("implied_claims", {}) or {}

    return HumanizationConfig(
        enabled=_coerce_bool(enabled_raw),
        markers_path=Path(
            os.getenv(
                "CCE_HUMANIZATION_MARKERS_PATH",
                file.get("markers_path", "config/humanization_markers.yaml"),
            )
        ),
        thresholds=HumanizationThresholds(**thresholds_data),
        editor=EditorConfig(**editor_data),
        implied_claims=ImpliedClaimsConfig(**implied_data),
    )


def _load_api_config(file: dict) -> APIConfig:
    require_auth_raw = os.getenv("CCE_API_REQUIRE_AUTH", file.get("require_auth", True))

    cors_raw = os.getenv("CCE_API_CORS_ORIGINS", None)
    if cors_raw is not None:
        cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
    else:
        cors_origins = file.get("cors_origins", ["*"])

    return APIConfig(
        host=os.getenv("CCE_API_HOST", file.get("host", "0.0.0.0")),
        port=int(os.getenv("CCE_API_PORT", file.get("port", 8000))),
        require_auth=_coerce_bool(require_auth_raw),
        cors_origins=cors_origins,
        max_concurrent_jobs=int(
            os.getenv(
                "CCE_API_MAX_CONCURRENT_JOBS",
                file.get("max_concurrent_jobs", 2),
            )
        ),
    )
