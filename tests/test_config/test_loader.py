"""Tests for cce.config.loader — YAML loading, env var precedence, type coercion."""

import pytest
import yaml

from cce.config.loader import load_config

pytestmark = pytest.mark.unit

# Env vars that could interfere with defaults
_ENV_VARS = [
    "CCE_LLM_PROVIDER",
    "CCE_LLM_MODEL",
    "CCE_LLM_API_KEY",
    "CCE_LLM_TEMPERATURE",
    "CCE_LLM_MAX_TOKENS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "CCE_EVIDENCE_BACKEND",
    "CCE_EVIDENCE_SQLITE_PATH",
    "CCE_CRAWL_ADAPTER",
    "CCE_CRAWL_API_KEY",
    "FIRECRAWL_API_KEY",
    "CCE_CRAWL_RATE_LIMIT",
    "CCE_CRAWL_TIMEOUT",
    "CCE_API_HOST",
    "CCE_API_PORT",
    "CCE_API_REQUIRE_AUTH",
    "CCE_API_CORS_ORIGINS",
    "CCE_API_MAX_CONCURRENT_JOBS",
]


def _clear_env(monkeypatch):
    """Remove all CCE/Anthropic/Firecrawl env vars for deterministic defaults."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_load_config_defaults(monkeypatch):
    _clear_env(monkeypatch)
    config = load_config()

    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-sonnet-4-6"
    assert config.llm.api_key == ""
    assert config.evidence_store.backend == "sqlite"
    assert config.crawl.adapter == "firecrawl"
    assert "low" in config.quality_gate
    assert "medium" in config.quality_gate
    assert "high" in config.quality_gate


def test_load_config_from_yaml(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump({"llm": {"model": "claude-opus-4-6", "api_key": "yaml-key"}})
    )
    config = load_config(config_file)

    assert config.llm.model == "claude-opus-4-6"
    assert config.llm.api_key == "yaml-key"


def test_load_config_env_overrides_yaml(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"llm": {"model": "claude-haiku-4-5-20251001"}}))
    monkeypatch.setenv("CCE_LLM_MODEL", "claude-opus-4-6")

    config = load_config(config_file)
    assert config.llm.model == "claude-opus-4-6"  # env var wins


def test_load_config_env_var_fallback_chain(monkeypatch):
    _clear_env(monkeypatch)
    # CCE_LLM_API_KEY not set, but ANTHROPIC_API_KEY is
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = load_config()
    assert config.llm.api_key == "test-key"


def test_load_config_missing_yaml(monkeypatch):
    _clear_env(monkeypatch)
    # Should not crash — uses defaults
    config = load_config("/nonexistent/path/config.yaml")
    assert config.llm.provider == "anthropic"


def test_load_gate_config_defaults(monkeypatch):
    _clear_env(monkeypatch)
    config = load_config()
    gate = config.quality_gate

    assert gate["low"].autopublish_threshold == 0.7
    assert gate["low"].min_citations_per_paragraph == 1
    assert gate["low"].max_writer_iterations == 2

    assert gate["medium"].autopublish_threshold == 0.85
    assert gate["medium"].min_citations_per_paragraph == 1
    assert gate["medium"].max_writer_iterations == 3

    assert gate["high"].autopublish_threshold == 0.95
    assert gate["high"].min_citations_per_paragraph == 2
    assert gate["high"].max_writer_iterations == 4


def test_load_gate_config_custom_profile(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump({"quality_gate": {"ultra": {"autopublish_threshold": 0.99}}})
    )
    config = load_config(config_file)

    assert "ultra" in config.quality_gate
    assert config.quality_gate["ultra"].autopublish_threshold == 0.99
    # Defaults should still be present
    assert "low" in config.quality_gate
    assert "medium" in config.quality_gate
    assert "high" in config.quality_gate


def test_load_config_type_coercion(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CCE_LLM_TEMPERATURE", "0.5")
    monkeypatch.setenv("CCE_LLM_MAX_TOKENS", "8192")

    config = load_config()
    assert isinstance(config.llm.temperature, float)
    assert config.llm.temperature == 0.5
    assert isinstance(config.llm.max_tokens, int)
    assert config.llm.max_tokens == 8192


def test_load_embedding_config_defaults(monkeypatch):
    _clear_env(monkeypatch)
    config = load_config()
    emb = config.embedding

    assert emb.enabled is True
    assert emb.provider == "ollama"
    assert emb.model == "nomic-embed-text-v2-moe"
    assert emb.dimensions == 768
    assert emb.base_url == "http://localhost:11434"
    assert emb.batch_size == 64


def test_load_embedding_config_env_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CCE_EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("CCE_EMBEDDING_MODEL", "custom-model")
    monkeypatch.setenv("CCE_EMBEDDING_BASE_URL", "http://remote:8080")
    monkeypatch.setenv("CCE_EMBEDDING_DIMENSIONS", "384")

    config = load_config()
    emb = config.embedding

    assert emb.enabled is False
    assert emb.model == "custom-model"
    assert emb.base_url == "http://remote:8080"
    assert emb.dimensions == 384


# ---------------------------------------------------------------------------
# API config (Phase 3)
# ---------------------------------------------------------------------------


def test_load_api_config_defaults(monkeypatch):
    _clear_env(monkeypatch)
    config = load_config()
    api = config.api

    assert api.host == "0.0.0.0"
    assert api.port == 8000
    assert api.require_auth is True
    assert api.cors_origins == ["*"]
    assert api.max_concurrent_jobs == 2


def test_load_api_config_env_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CCE_API_PORT", "9000")
    monkeypatch.setenv("CCE_API_REQUIRE_AUTH", "false")
    monkeypatch.setenv("CCE_API_MAX_CONCURRENT_JOBS", "5")
    monkeypatch.setenv("CCE_API_HOST", "127.0.0.1")

    config = load_config()
    api = config.api

    assert api.host == "127.0.0.1"
    assert api.port == 9000
    assert api.require_auth is False
    assert api.max_concurrent_jobs == 5


def test_load_api_config_cors_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CCE_API_CORS_ORIGINS", "http://localhost:3000, https://app.example.com")

    config = load_config()
    assert config.api.cors_origins == ["http://localhost:3000", "https://app.example.com"]


def test_load_api_config_from_yaml(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump({"api": {"port": 9999, "require_auth": False, "host": "0.0.0.0"}})
    )
    config = load_config(config_file)

    assert config.api.port == 9999
    assert config.api.require_auth is False
