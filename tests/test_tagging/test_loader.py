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
