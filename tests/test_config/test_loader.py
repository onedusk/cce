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
