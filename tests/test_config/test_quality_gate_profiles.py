"""Tests for the single-source QUALITY_GATE_PROFILES templates (audit A3, T-06.01)."""

from __future__ import annotations

import pytest

from cce.config.loader import _load_gate_config
from cce.config.types import (
    QUALITY_GATE_PROFILES,
    QualityGateConfig,
    default_quality_gate_profiles,
)

pytestmark = pytest.mark.unit


def test_profiles_constant_has_three_named_profiles():
    assert set(QUALITY_GATE_PROFILES.keys()) == {"low", "medium", "high"}


def test_default_profiles_factory_builds_fresh_dict_each_call():
    a = default_quality_gate_profiles()
    b = default_quality_gate_profiles()
    assert a == b
    # Different object identity — the factory returns a new dict each time
    # so test mutations don't bleed between tests.
    assert a is not b


def test_medium_defaults_match_canonical():
    profiles = default_quality_gate_profiles()
    assert profiles["medium"].max_writer_iterations == 3
    assert profiles["medium"].min_citations_per_paragraph == 1
    assert profiles["medium"].autopublish_threshold == 0.85


def test_high_defaults_match_canonical():
    profiles = default_quality_gate_profiles()
    assert profiles["high"].max_writer_iterations == 4
    assert profiles["high"].min_citations_per_paragraph == 2
    assert profiles["high"].autopublish_threshold == 0.95


def test_low_defaults_match_canonical():
    profiles = default_quality_gate_profiles()
    assert profiles["low"].max_writer_iterations == 2
    assert profiles["low"].min_citations_per_paragraph == 1
    assert profiles["low"].autopublish_threshold == 0.7


def test_loader_with_empty_file_returns_canonical_defaults():
    """No YAML overrides -> result identical to default_quality_gate_profiles()."""
    result = _load_gate_config({})
    expected = default_quality_gate_profiles()
    # Compare by serialized dict because QualityGateConfig instances aren't
    # directly `==`-compared by value across instantiations.
    assert {k: v.model_dump() for k, v in result.items()} == {
        k: v.model_dump() for k, v in expected.items()
    }


def test_loader_yaml_override_replaces_profile():
    result = _load_gate_config({"high": {"autopublish_threshold": 0.99}})
    # Explicit override: the overridden profile takes the new value.
    assert result["high"].autopublish_threshold == 0.99
    # Non-overridden profiles stay at the canonical values.
    assert result["low"].autopublish_threshold == 0.7
    assert result["medium"].autopublish_threshold == 0.85


def test_loader_ignores_non_dict_yaml_entries():
    """A malformed profile value (e.g. a list or scalar) is silently skipped."""
    result = _load_gate_config({"high": "not a dict"})  # type: ignore[arg-type]
    # High stays at canonical defaults.
    assert result["high"].autopublish_threshold == 0.95


def test_single_source_propagates_to_engine_config_default():
    """EngineConfig.quality_gate default goes through the same factory."""
    from cce.config.types import EngineConfig, LLMConfig

    cfg = EngineConfig(llm=LLMConfig(api_key="test"))
    expected = default_quality_gate_profiles()
    assert set(cfg.quality_gate.keys()) == set(expected.keys())
    for name in expected:
        assert cfg.quality_gate[name].model_dump() == expected[name].model_dump()
