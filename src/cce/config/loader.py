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


def _opt(cast: Any, value: Any) -> Any:
    """Coerce a value when present; None stays None (= 'not configured')."""
    return None if value is None else cast(value)


def _explicit(**candidates: Any) -> dict[str, Any]:
    """Keep only explicitly-configured values (finding 1.4). None means the
    key was set neither in env nor YAML, so it is dropped and the
    ``types.py`` Field default applies — the single source of defaults."""
    return {k: v for k, v in candidates.items() if v is not None}


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

    kwargs = _explicit(engine_version=file_data.get("engine_version"))
    return EngineConfig(
        llm=_load_llm_config(file_data.get("llm", {})),
        evidence_store=_load_evidence_config(file_data.get("evidence_store", {})),
        crawl=_load_crawl_config(file_data.get("crawl", {})),
        embedding=_load_embedding_config(file_data.get("embedding", {})),
        quality_gate=_load_gate_config(file_data.get("quality_gate", {})),
        api=_load_api_config(file_data.get("api", {})),
        humanization=_load_humanization_config(file_data.get("humanization", {})),
        **kwargs,
    )


def _load_llm_config(file: dict) -> LLMConfig:
    # api_key is passed unconditionally: it is the one required field on
    # LLMConfig (no types.py default) and "" is the legitimate keyless value
    # (ADR-006 — emit-mdx / api key generate run without it).
    return LLMConfig(
        api_key=os.getenv("CCE_LLM_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or file.get("api_key", ""),
        **_explicit(
            provider=os.getenv("CCE_LLM_PROVIDER", file.get("provider")),
            model=os.getenv("CCE_LLM_MODEL")
            or os.getenv("ANTHROPIC_MODEL")
            or file.get("model"),
            temperature=_opt(
                float, os.getenv("CCE_LLM_TEMPERATURE", file.get("temperature"))
            ),
            max_tokens=_opt(
                int, os.getenv("CCE_LLM_MAX_TOKENS", file.get("max_tokens"))
            ),
        ),
    )


def _load_evidence_config(file: dict) -> EvidenceStoreConfig:
    return EvidenceStoreConfig(
        **_explicit(
            backend=os.getenv("CCE_EVIDENCE_BACKEND", file.get("backend")),
            sqlite_path=_opt(
                Path, os.getenv("CCE_EVIDENCE_SQLITE_PATH", file.get("sqlite_path"))
            ),
        )
    )


def _load_crawl_config(file: dict) -> CrawlConfig:
    return CrawlConfig(
        **_explicit(
            adapter=os.getenv("CCE_CRAWL_ADAPTER", file.get("adapter")),
            api_key=os.getenv("CCE_CRAWL_API_KEY")
            or os.getenv("FIRECRAWL_API_KEY")
            or file.get("api_key"),
            rate_limit_rps=_opt(
                float, os.getenv("CCE_CRAWL_RATE_LIMIT", file.get("rate_limit_rps"))
            ),
            timeout_seconds=_opt(
                int, os.getenv("CCE_CRAWL_TIMEOUT", file.get("timeout_seconds"))
            ),
            max_excerpts_per_source=_opt(
                int,
                os.getenv(
                    "CCE_CRAWL_MAX_PER_SOURCE", file.get("max_excerpts_per_source")
                ),
            ),
            max_evidence_total=_opt(
                int,
                os.getenv("CCE_CRAWL_MAX_EVIDENCE", file.get("max_evidence_total")),
            ),
        )
    )


def _load_embedding_config(file: dict) -> EmbeddingConfig:
    return EmbeddingConfig(
        **_explicit(
            enabled=_opt(
                _coerce_bool, os.getenv("CCE_EMBEDDING_ENABLED", file.get("enabled"))
            ),
            provider=os.getenv("CCE_EMBEDDING_PROVIDER", file.get("provider")),
            model=os.getenv("CCE_EMBEDDING_MODEL", file.get("model")),
            dimensions=_opt(
                int, os.getenv("CCE_EMBEDDING_DIMENSIONS", file.get("dimensions"))
            ),
            base_url=os.getenv("CCE_EMBEDDING_BASE_URL", file.get("base_url")),
            timeout_seconds=_opt(
                int, os.getenv("CCE_EMBEDDING_TIMEOUT", file.get("timeout_seconds"))
            ),
            batch_size=_opt(
                int, os.getenv("CCE_EMBEDDING_BATCH_SIZE", file.get("batch_size"))
            ),
        )
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
    thresholds_data = file.get("thresholds", {}) or {}
    editor_data = file.get("editor", {}) or {}
    implied_data = file.get("implied_claims", {}) or {}

    return HumanizationConfig(
        **_explicit(
            enabled=_opt(
                _coerce_bool,
                os.getenv("CCE_HUMANIZATION_ENABLED", file.get("enabled")),
            ),
            markers_path=_opt(
                Path,
                os.getenv("CCE_HUMANIZATION_MARKERS_PATH", file.get("markers_path")),
            ),
            thresholds=(
                HumanizationThresholds(**thresholds_data) if thresholds_data else None
            ),
            editor=EditorConfig(**editor_data) if editor_data else None,
            implied_claims=(
                ImpliedClaimsConfig(**implied_data) if implied_data else None
            ),
        )
    )


def _load_api_config(file: dict) -> APIConfig:
    cors_raw = os.getenv("CCE_API_CORS_ORIGINS", None)
    if cors_raw is not None:
        cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
    else:
        cors_origins = file.get("cors_origins")

    return APIConfig(
        **_explicit(
            host=os.getenv("CCE_API_HOST", file.get("host")),
            port=_opt(int, os.getenv("CCE_API_PORT", file.get("port"))),
            require_auth=_opt(
                _coerce_bool,
                os.getenv("CCE_API_REQUIRE_AUTH", file.get("require_auth")),
            ),
            cors_origins=cors_origins,
            max_concurrent_jobs=_opt(
                int,
                os.getenv(
                    "CCE_API_MAX_CONCURRENT_JOBS", file.get("max_concurrent_jobs")
                ),
            ),
        )
    )
