"""Tests for humanization config plumbing (M01): typed config, loader overlay,
env-var coercion, and the marker-YAML loader."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cce.config.loader import load_config
from cce.config.markers import HumanizationMarkers, load_markers
from cce.config.types import (
    EditorConfig,
    HumanizationConfig,
    HumanizationThresholds,
    ImpliedClaimsConfig,
)

pytestmark = pytest.mark.unit

_HUMANIZATION_ENV_VARS = (
    "CCE_HUMANIZATION_ENABLED",
    "CCE_HUMANIZATION_MARKERS_PATH",
)


def _clear_env(monkeypatch):
    for var in _HUMANIZATION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_humanization_config_defaults():
    """Defaults: everything off, marker path in config/ directory."""
    cfg = HumanizationConfig()

    assert cfg.enabled is False
    assert cfg.markers_path == Path("config/humanization_markers.yaml")
    assert isinstance(cfg.thresholds, HumanizationThresholds)
    assert isinstance(cfg.editor, EditorConfig)
    assert isinstance(cfg.implied_claims, ImpliedClaimsConfig)
    assert cfg.thresholds.min_sentence_length_stddev == 8.0
    assert cfg.editor.enabled is False
    assert cfg.editor.temperature == 0.4
    assert cfg.implied_claims.enabled is False
    assert cfg.implied_claims.search_strategy == "llm_extract"


def test_humanization_config_yaml_overlay(monkeypatch, tmp_path):
    """YAML entries override defaults per-field; unspecified fields keep defaults."""
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "humanization": {
                    "enabled": True,
                    "thresholds": {"min_sentence_length_stddev": 6.5},
                    "editor": {"temperature": 0.7},
                }
            }
        )
    )
    cfg = load_config(config_file)

    assert cfg.humanization.enabled is True
    assert cfg.humanization.thresholds.min_sentence_length_stddev == 6.5
    assert cfg.humanization.editor.temperature == 0.7
    # Unspecified threshold stays at default
    assert cfg.humanization.thresholds.min_type_token_ratio == 0.45
    # Unspecified editor field stays at default
    assert cfg.humanization.editor.enabled is False


def test_humanization_env_var_overrides_yaml(monkeypatch, tmp_path):
    """CCE_HUMANIZATION_ENABLED wins over the YAML value."""
    _clear_env(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"humanization": {"enabled": False}}))
    monkeypatch.setenv("CCE_HUMANIZATION_ENABLED", "true")

    cfg = load_config(config_file)

    assert cfg.humanization.enabled is True


def test_load_markers_returns_seeded_lists():
    """The checked-in marker YAML has the expected research-grounded lists."""
    markers = load_markers("config/humanization_markers.yaml")

    assert isinstance(markers, HumanizationMarkers)
    # Juzek/Ward 21 + Stanford 8 with some plurals ≈ 26 entries
    assert len(markers.suppressed_vocabulary) >= 25
    assert "delve" in markers.suppressed_vocabulary
    assert "additionally" in markers.suppressed_vocabulary
    assert markers.hedging_phrases
    assert markers.formulaic_transitions
    assert markers.contrastive_patterns


def test_load_markers_missing_file_raises(tmp_path):
    """Silent fallback is wrong — operators who enabled humanization
    expected the file."""
    missing = tmp_path / "nope.yaml"

    with pytest.raises(FileNotFoundError):
        load_markers(missing)


def test_compiled_contrastive_patterns_match_known_ai_prose():
    """Regex compiles AND the patterns catch the exemplars from
    docs/internal/research/contrastive_framing_as_implied_claims.md."""
    markers = load_markers("config/humanization_markers.yaml")
    patterns = markers.compiled_contrastive_patterns()

    assert patterns
    assert all(isinstance(p, re.Pattern) for p in patterns)

    # Each exemplar should match at least one compiled pattern
    exemplars = [
        "Unlike sleeping pills, CBT-I addresses the underlying causes",
        "it's not about speed, it's about quality",
        "rather than sedating you past the problem",
        "not just insomnia, but sleep hygiene broadly",
    ]
    for text in exemplars:
        assert any(p.search(text) for p in patterns), f"no pattern matched: {text!r}"
