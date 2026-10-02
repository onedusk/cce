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
    """Defaults: full stack ON (operator preference 2026-06-24); no marker
    path set, so the registry picks the override file or the packaged lists."""
    cfg = HumanizationConfig()

    assert cfg.enabled is True
    assert cfg.markers_path is None
    assert isinstance(cfg.thresholds, HumanizationThresholds)
    assert isinstance(cfg.editor, EditorConfig)
    assert isinstance(cfg.implied_claims, ImpliedClaimsConfig)
    assert cfg.thresholds.min_sentence_length_stddev == 10.0
    assert cfg.thresholds.min_type_token_ratio == 0.38
    assert cfg.editor.enabled is True
    assert cfg.editor.temperature == 0.4
    assert cfg.implied_claims.enabled is True
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
    # Unspecified threshold stays at the calibrated default (0.38, not 0.45)
    assert cfg.humanization.thresholds.min_type_token_ratio == 0.38
    # Unspecified editor field stays at default (now ON by default)
    assert cfg.humanization.editor.enabled is True


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
    markers = load_markers()

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
    docs/internal/research/contrastive_framing_as_implied_claims.md.

    After 0.2.0 the compiled list is ``list[tuple[Pattern, subtype]]``
    where subtype is ``"parasitic"`` or ``"genuine_alternative"``.
    """
    markers = load_markers()
    patterns = markers.compiled_contrastive_patterns()

    assert patterns
    assert all(
        isinstance(p, re.Pattern) and s in {"parasitic", "genuine_alternative"}
        for p, s in patterns
    )

    # Each exemplar should match at least one compiled pattern (any subtype).
    exemplars = [
        "Unlike sleeping pills, CBT-I addresses the underlying causes",
        "it's not about speed, it's about quality",
        "rather than sedating you past the problem",
        "not just insomnia, but sleep hygiene broadly",
    ]
    for text in exemplars:
        assert any(p.search(text) for p, _ in patterns), f"no pattern matched: {text!r}"


def test_parasitic_patterns_tagged_and_match_reframe_construction():
    """Parasitic period-split and comma-split patterns land under the
    ``parasitic`` subtype and catch the canonical reframe shape."""
    markers = load_markers()
    patterns = markers.compiled_contrastive_patterns()

    parasitic = [p for p, s in patterns if s == "parasitic"]
    genuine = [p for p, s in patterns if s == "genuine_alternative"]
    assert len(parasitic) >= 2, "expected ≥2 parasitic patterns (period + comma split)"
    assert len(genuine) >= 4, "expected existing genuine_alternative patterns preserved"

    # Canonical parasitic exemplar (the original trigger case — Phase B analysis)
    assert any(
        p.search(
            "What replaces the open question is not wisdom. It is the illusion of it."
        )
        for p in parasitic
    )
    # Canonical corpus-drawn parasitic
    assert any(
        p.search("boredom is not a problem to be solved. It is a signal to be heard.")
        for p in parasitic
    )
    # Genuine-alternative exemplar must NOT be caught by parasitic patterns
    assert not any(
        p.search("Unlike sleeping pills, CBT-I addresses the underlying causes")
        for p in parasitic
    )


# --- Packaged marker lists (audit 3.1: the file was not in the wheel) --------


def _registry_in(root, **humanization):
    from cce.config.registry import ConfigRegistry
    from cce.config.types import EngineConfig, LLMConfig

    engine = EngineConfig(
        llm=LLMConfig(api_key="k"), humanization=HumanizationConfig(**humanization)
    )
    return ConfigRegistry.load(root, engine=engine)


def test_packaged_markers_load_with_no_file_in_the_working_directory(tmp_path):
    """A consumer that installs cce as a dependency has no config/ directory:
    humanization (on by default) must still boot, on the packaged lists."""
    packaged = load_markers()

    assert packaged.suppressed_vocabulary and packaged.contrastive_patterns
    assert packaged.compiled_contrastive_patterns()  # every regex compiles
    assert _registry_in(tmp_path).markers == packaged


def test_working_directory_markers_file_replaces_the_packaged_lists(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "humanization_markers.yaml").write_text(
        "suppressed_vocabulary: [synergy]\n"
    )
    markers = _registry_in(tmp_path).markers

    assert markers.suppressed_vocabulary == ["synergy"]
    assert markers.contrastive_patterns == []  # replaced whole, not merged


def test_explicit_markers_path_that_is_missing_still_fails_fast(tmp_path):
    from cce.config.loader import ConfigError

    with pytest.raises(ConfigError, match="markers file not found"):
        _registry_in(tmp_path, markers_path=Path("config/nope.yaml"))
