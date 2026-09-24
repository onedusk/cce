"""Integration tests for ConfigRegistry (audit-2026-06-09 M06, ADR-002).

Each test builds its own tmp_path tree — the composed loaders are
``lru_cache``d on their path arguments, so distinct trees per test keep the
caches from leaking state between tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cce.config.loader import ConfigError
from cce.config.registry import ConfigRegistry
from cce.config.types import EngineConfig, HumanizationConfig, LLMConfig

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tree builders
# ---------------------------------------------------------------------------


def _write_policy(root: Path, policy_id: str) -> None:
    policies = root / "policies"
    policies.mkdir(exist_ok=True)
    (policies / f"{policy_id}.yaml").write_text(
        f"id: {policy_id}\nname: {policy_id.title()}\n"
    )


def _write_path_configs(root: Path, filename: str, path_ids: list[str]) -> None:
    pc_dir = root / "path_configs"
    pc_dir.mkdir(exist_ok=True)
    items = "\n".join(
        f"- id: {pid}\n  name: {pid.title()}\n  description: {pid} path"
        for pid in path_ids
    )
    (pc_dir / filename).write_text(items + "\n")


def _write_taxonomy(root: Path, dirname: str = "taxonomies") -> Path:
    tax_dir = root / dirname
    tax_dir.mkdir(exist_ok=True)
    tax_path = tax_dir / "wellbeing-8d.yaml"
    tax_path.write_text("id: wellbeing-8d\nname: Well-Being 8D\ndimensions: []\n")
    return tax_path


def _write_markers(root: Path) -> Path:
    markers_path = root / "markers.yaml"
    markers_path.write_text("suppressed_vocabulary:\n  - delve\n")
    return markers_path


# ---------------------------------------------------------------------------
# Full load
# ---------------------------------------------------------------------------


async def test_full_load_round_trips_fixture_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """All four surfaces load; thnklabs.yaml is preferred over default.yaml."""
    monkeypatch.delenv("CCE_HUMANIZATION_ENABLED", raising=False)
    monkeypatch.delenv("CCE_HUMANIZATION_MARKERS_PATH", raising=False)

    _write_policy(tmp_path, "strict")
    _write_policy(tmp_path, "relaxed")
    _write_path_configs(tmp_path, "default.yaml", ["blog"])
    _write_path_configs(tmp_path, "thnklabs.yaml", ["essay", "faq"])
    tax_path = _write_taxonomy(tmp_path)
    markers_path = _write_markers(tmp_path)

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"humanization:\n  enabled: true\n  markers_path: {markers_path}\n"
    )

    registry = ConfigRegistry.load(tmp_path, config_yaml)

    assert sorted(registry.policies) == ["relaxed", "strict"]
    assert sorted(registry.path_configs) == ["essay", "faq"]  # thnklabs preferred
    assert registry.taxonomy_path == tax_path
    assert registry.markers is not None
    assert registry.markers.suppressed_vocabulary == ["delve"]
    assert registry.engine.humanization.enabled is True


async def test_missing_optional_dirs_tolerated(tmp_path: Path):
    """Bare root → empty dicts / None, matching the lifespan tolerance."""
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(tmp_path, engine=engine)

    assert registry.policies == {}
    assert registry.path_configs == {}
    assert registry.taxonomy_path is None
    assert registry.markers is None


async def test_path_configs_fall_back_to_default(tmp_path: Path):
    """No thnklabs.yaml → default.yaml is selected."""
    _write_path_configs(tmp_path, "default.yaml", ["blog"])
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(tmp_path, engine=engine)

    assert sorted(registry.path_configs) == ["blog"]


# ---------------------------------------------------------------------------
# Dead-params fix: explicit dirs are honored again
# ---------------------------------------------------------------------------


async def test_explicit_path_configs_path_honored(tmp_path: Path):
    """path_configs_path overrides the thnklabs/default selection."""
    _write_path_configs(tmp_path, "thnklabs.yaml", ["essay"])
    custom = tmp_path / "custom_paths.yaml"
    custom.write_text("- id: digest\n  name: Digest\n  description: digest path\n")
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(
        tmp_path, engine=engine, path_configs_path=Path("custom_paths.yaml")
    )

    assert sorted(registry.path_configs) == ["digest"]


async def test_explicit_taxonomies_dir_honored(tmp_path: Path):
    """taxonomies_dir overrides the default taxonomies/ location."""
    tax_path = _write_taxonomy(tmp_path, dirname="alt_taxonomies")
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(
        tmp_path, engine=engine, taxonomies_dir=Path("alt_taxonomies")
    )

    assert registry.taxonomy_path == tax_path


async def test_explicit_policies_dir_honored(tmp_path: Path):
    """policies_dir overrides the default policies/ location."""
    alt = tmp_path / "alt_policies"
    alt.mkdir()
    (alt / "custom.yaml").write_text("id: custom\nname: Custom\n")
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(
        tmp_path, engine=engine, policies_dir=Path("alt_policies")
    )

    assert sorted(registry.policies) == ["custom"]


# ---------------------------------------------------------------------------
# Forgiving policy load (PDR-003) and fail-fast markers
# ---------------------------------------------------------------------------


async def test_policies_loader_exception_warns_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """An exception out of load_policies degrades to {} with a warning."""
    (tmp_path / "policies").mkdir()

    def _boom(directory):
        raise OSError("unreadable directory")

    monkeypatch.setattr("cce.config.registry.load_policies", _boom)
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )

    registry = ConfigRegistry.load(tmp_path, engine=engine)

    assert registry.policies == {}
    assert "Failed to load policies" in caplog.text


async def test_missing_markers_raises_when_humanization_enabled(tmp_path: Path):
    """Markers stay fail-fast: enabled humanization + missing YAML raises
    ConfigError (not FileNotFoundError) so every CLI/app entry point renders
    one actionable line (final-review finding 2, 2026-06-09)."""
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(
            enabled=True, markers_path=Path("nope/markers.yaml")
        ),
    )

    with pytest.raises(ConfigError, match="markers"):
        ConfigRegistry.load(tmp_path, engine=engine)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


async def test_get_policy_keyerror_lists_known_ids(tmp_path: Path):
    _write_policy(tmp_path, "strict")
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )
    registry = ConfigRegistry.load(tmp_path, engine=engine)

    assert registry.get_policy("strict").id == "strict"
    with pytest.raises(KeyError, match=r"Unknown policy 'nope'.*strict"):
        registry.get_policy("nope")


async def test_get_path_config_keyerror_lists_known_ids(tmp_path: Path):
    _write_path_configs(tmp_path, "default.yaml", ["blog"])
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )
    registry = ConfigRegistry.load(tmp_path, engine=engine)

    assert registry.get_path_config("blog").id == "blog"
    with pytest.raises(KeyError, match=r"Unknown path 'nope'.*blog"):
        registry.get_path_config("nope")


async def test_accessors_report_none_loaded_on_empty_registry(tmp_path: Path):
    engine = EngineConfig(
        llm=LLMConfig(api_key=""),
        humanization=HumanizationConfig(enabled=False),
    )
    registry = ConfigRegistry.load(tmp_path, engine=engine)

    with pytest.raises(KeyError, match=r"\(none loaded\)"):
        registry.get_policy("anything")
    with pytest.raises(KeyError, match=r"\(none loaded\)"):
        registry.get_path_config("anything")


# ---------------------------------------------------------------------------
# Precedence smoke
# ---------------------------------------------------------------------------


async def test_env_overrides_yaml_for_engine_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Registry-loaded engine config keeps env > YAML precedence."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "llm:\n  model: yaml-model\nhumanization:\n  enabled: false\n"
    )
    monkeypatch.setenv("CCE_LLM_MODEL", "env-model")

    registry = ConfigRegistry.load(tmp_path, config_yaml)

    assert registry.engine.llm.model == "env-model"
