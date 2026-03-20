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
    CrawlConfig,
    EngineConfig,
    EvidenceStoreConfig,
    LLMConfig,
    QualityGateConfig,
)


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
        quality_gate=_load_gate_config(file_data.get("quality_gate", {})),
        engine_version=file_data.get("engine_version", "0.1.0"),
    )


def _load_llm_config(file: dict) -> LLMConfig:
    return LLMConfig(
        provider=os.getenv("CCE_LLM_PROVIDER", file.get("provider", "anthropic")),
        model=os.getenv("CCE_LLM_MODEL") or os.getenv("ANTHROPIC_MODEL") or file.get("model", "claude-sonnet-4-6"),
        api_key=os.getenv("CCE_LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or file.get("api_key", ""),
        temperature=float(
            os.getenv("CCE_LLM_TEMPERATURE", file.get("temperature", 0.2))
        ),
        max_tokens=int(
            os.getenv("CCE_LLM_MAX_TOKENS", file.get("max_tokens", 4096))
        ),
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
        api_key=os.getenv("CCE_CRAWL_API_KEY") or os.getenv("FIRECRAWL_API_KEY") or file.get("api_key"),
        rate_limit_rps=float(
            os.getenv("CCE_CRAWL_RATE_LIMIT", file.get("rate_limit_rps", 2.0))
        ),
        timeout_seconds=int(
            os.getenv("CCE_CRAWL_TIMEOUT", file.get("timeout_seconds", 30))
        ),
        max_excerpts_per_source=int(
            os.getenv("CCE_CRAWL_MAX_PER_SOURCE", file.get("max_excerpts_per_source", 5))
        ),
        max_evidence_total=int(
            os.getenv("CCE_CRAWL_MAX_EVIDENCE", file.get("max_evidence_total", 100))
        ),
    )


def _load_gate_config(file: dict) -> dict[str, QualityGateConfig]:
    """Load quality gate configs. Falls back to defaults if not in file."""
    defaults = {
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
    }

    if not file:
        return defaults

    result = dict(defaults)
    for profile_name, profile_data in file.items():
        if isinstance(profile_data, dict):
            result[profile_name] = QualityGateConfig(**profile_data)

    return result
