"""Tests for taxonomy and path config YAML loaders."""

from pathlib import Path

import yaml

from cce.tagging.loader import load_path_configs, load_taxonomy


def _write_yaml(tmp_path: Path, filename: str, data: dict | list) -> Path:
    p = tmp_path / filename
    p.write_text(yaml.dump(data))
    return p


class TestLoadTaxonomy:
    def test_basic(self, tmp_path):
        data = {
            "id": "test-tax",
            "name": "Test Taxonomy",
            "dimensions": [
                {
                    "id": "dim1",
                    "name": "Dimension 1",
                    "values": ["primary", "none"],
                },
                {
                    "id": "dim2",
                    "name": "Dimension 2",
                    "description": "Second dim",
                    "values": ["primary", "secondary", "none"],
                },
            ],
        }
        path = _write_yaml(tmp_path, "tax.yaml", data)
        tc = load_taxonomy(path)
        assert tc.id == "test-tax"
        assert tc.name == "Test Taxonomy"
        assert len(tc.dimensions) == 2
        assert tc.dimension_ids() == ["dim1", "dim2"]

    def test_name_falls_back_to_id(self, tmp_path):
        data = {
            "id": "fallback",
            "dimensions": [{"id": "a", "name": "A", "values": ["x"]}],
        }
        path = _write_yaml(tmp_path, "tax.yaml", data)
        tc = load_taxonomy(path)
        assert tc.name == "fallback"


class TestLoadPathConfigs:
    def test_with_paths_key(self, tmp_path):
        data = {
            "paths": [
                {"id": "learn", "name": "Learn", "tone": "pedagogical"},
                {"id": "apply", "name": "Apply", "structure": "actionable"},
            ]
        }
        path = _write_yaml(tmp_path, "paths.yaml", data)
        configs = load_path_configs(path)
        assert set(configs.keys()) == {"learn", "apply"}
        assert configs["learn"].tone == "pedagogical"
        assert configs["apply"].structure == "actionable"

    def test_bare_list(self, tmp_path):
        data = [
            {"id": "learn", "name": "Learn"},
            {"id": "explore", "name": "Explore"},
        ]
        path = _write_yaml(tmp_path, "paths.yaml", data)
        configs = load_path_configs(path)
        assert set(configs.keys()) == {"learn", "explore"}

    def test_single_object(self, tmp_path):
        data = {"id": "solo", "name": "Solo Path"}
        path = _write_yaml(tmp_path, "paths.yaml", data)
        configs = load_path_configs(path)
        assert "solo" in configs
        assert configs["solo"].name == "Solo Path"


# ---------------------------------------------------------------------------
# Graceful degradation (audit A4 / ADR-006)
# ---------------------------------------------------------------------------


class TestLoaderDegradation:
    """User-supplied YAML loaders catch parse errors and return a degraded
    result (None / empty dict) + warning log, rather than raising."""

    def test_taxonomy_malformed_yaml_returns_none(self, tmp_path, caplog):
        import logging

        path = tmp_path / "bad.yaml"
        path.write_text("key: [unclosed list")  # invalid YAML

        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_taxonomy(path)

        assert result is None
        assert any("Could not load taxonomy" in r.getMessage() for r in caplog.records)

    def test_taxonomy_missing_file_returns_none(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_taxonomy(tmp_path / "nonexistent.yaml")

        assert result is None
        assert any("Could not load taxonomy" in r.getMessage() for r in caplog.records)

    def test_taxonomy_missing_required_id_returns_none(self, tmp_path, caplog):
        import logging

        path = _write_yaml(tmp_path, "bad.yaml", {"name": "No ID"})
        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_taxonomy(path)

        assert result is None
        assert any("Malformed taxonomy" in r.getMessage() for r in caplog.records)

    def test_path_configs_malformed_yaml_returns_empty(self, tmp_path, caplog):
        import logging

        path = tmp_path / "bad.yaml"
        path.write_text("key: [unclosed list")

        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_path_configs(path)

        assert result == {}
        assert any(
            "Could not load path configs" in r.getMessage() for r in caplog.records
        )

    def test_path_configs_unexpected_structure_returns_empty(self, tmp_path, caplog):
        import logging

        # Top-level scalar — neither dict nor list.
        path = tmp_path / "bad.yaml"
        path.write_text("just-a-string")

        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_path_configs(path)

        assert result == {}
        assert any(
            "Unexpected YAML structure" in r.getMessage() for r in caplog.records
        )

    def test_cache_reuses_parsed_result(self, tmp_path):
        """Second call returns the same object by identity — proof of cache hit."""
        load_taxonomy.cache_clear()
        data = {
            "id": "cached-tax",
            "name": "Cached",
            "dimensions": [{"id": "d1", "name": "D1", "values": ["a"]}],
        }
        path = _write_yaml(tmp_path, "cached.yaml", data)
        first = load_taxonomy(path)
        second = load_taxonomy(path)
        assert first is second  # identity — same cached object

    def test_cache_clear_forces_reparse(self, tmp_path):
        """After cache_clear(), the next call returns a freshly-parsed object."""
        load_taxonomy.cache_clear()
        data = {
            "id": "tax",
            "name": "Tax",
            "dimensions": [{"id": "d1", "name": "D1", "values": ["a"]}],
        }
        path = _write_yaml(tmp_path, "tax.yaml", data)
        first = load_taxonomy(path)
        load_taxonomy.cache_clear()
        second = load_taxonomy(path)
        assert first is not second  # cache miss -> fresh instance
        assert first.id == second.id  # but equivalent content

    def test_path_configs_malformed_item_keeps_valid_predecessors(
        self, tmp_path, caplog
    ):
        """If item 2 is malformed, items before it stay in the result."""
        import logging

        data = [
            {"id": "good", "name": "Good Path"},
            {"name": "missing id"},  # required field missing
            {"id": "never-reached", "name": "Never Reached"},
        ]
        path = _write_yaml(tmp_path, "paths.yaml", data)
        with caplog.at_level(logging.WARNING, logger="cce.tagging.loader"):
            result = load_path_configs(path)

        assert "good" in result
        # "never-reached" comes AFTER the bad item and will not be processed
        # because the for-loop raised. This is intentional — we preserve
        # predecessors but stop on the first bad one.
        assert "never-reached" not in result
        assert any("Malformed path config" in r.getMessage() for r in caplog.records)
