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


def load_taxonomy(path: str | Path) -> TaxonomyConfig | None:
    """Load a single TaxonomyConfig from a YAML file.

    User-supplied loader per ADR-006: catches parse and I/O errors, logs a
    warning, and returns None so callers can gracefully run without a
    taxonomy rather than bricking on a typo. Strict validation errors
    surfaced by Pydantic also degrade to None — same reasoning.
    """
    path = Path(path)
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return _parse_taxonomy(data)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Could not load taxonomy from %s: %s", path, e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Malformed taxonomy YAML at %s: %s", path, e)
        return None


def load_path_configs(path: str | Path) -> dict[str, PathConfig]:
    """Load path configs from a YAML file.

    The file should contain a list of path config objects, or a single
    object, or a container with a "paths" key. User-supplied loader per
    ADR-006: returns an empty dict on any parse/structure/validation
    failure and logs a warning, so the pipeline runs without path configs
    rather than failing.
    """
    path = Path(path)
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Could not load path configs from %s: %s", path, e)
        return {}

    if isinstance(data, dict):
        items = data["paths"] if "paths" in data else [data]
    elif isinstance(data, list):
        items = data
    else:
        logger.warning(
            "Unexpected YAML structure in %s (expected dict or list, got %s)",
            path,
            type(data).__name__,
        )
        return {}

    result: dict[str, PathConfig] = {}
    try:
        for item in items:
            pc = PathConfig(**item)
            result[pc.id] = pc
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(
            "Malformed path config in %s (%d kept before error): %s",
            path,
            len(result),
            e,
        )
    return result


def _parse_taxonomy(data: dict) -> TaxonomyConfig:
    """Parse a taxonomy dict from YAML into a TaxonomyConfig."""
    dimensions = [Dimension(**dim_data) for dim_data in data.get("dimensions", [])]
    return TaxonomyConfig(
        id=data["id"],
        name=data.get("name", data["id"]),
        dimensions=dimensions,
    )
