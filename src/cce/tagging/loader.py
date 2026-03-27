"""Taxonomy and path config loaders.

Loads TaxonomyConfig and PathConfig definitions from YAML files.
Follows the same pattern as policy/loader.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from cce.models.paths import PathConfig
from cce.models.taxonomy import Dimension, TaxonomyConfig

logger = logging.getLogger(__name__)


def load_taxonomy(path: str | Path) -> TaxonomyConfig:
    """Load a single TaxonomyConfig from a YAML file."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)
    return _parse_taxonomy(data)


def load_path_configs(path: str | Path) -> dict[str, PathConfig]:
    """Load path configs from a YAML file.

    The file should contain a list of path config objects, or a single object.
    Returns a dict keyed by PathConfig.id.
    """
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict):
        # Single path config or a container with a "paths" key
        if "paths" in data:
            items = data["paths"]
        else:
            items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(f"Unexpected YAML structure in {path}")

    result: dict[str, PathConfig] = {}
    for item in items:
        pc = PathConfig(**item)
        result[pc.id] = pc

    return result


def _parse_taxonomy(data: dict) -> TaxonomyConfig:
    """Parse a taxonomy dict from YAML into a TaxonomyConfig."""
    dimensions = [Dimension(**dim_data) for dim_data in data.get("dimensions", [])]
    return TaxonomyConfig(
        id=data["id"],
        name=data.get("name", data["id"]),
        dimensions=dimensions,
    )
