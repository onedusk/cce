"""Loader for the humanization markers YAML.

Kept separate from config/types.py so the marker file can grow large without
bloating the typed-config module. Loaded once at engine construction; the
coevolution of AI markers (documented in docs/internal/research/) means this
list updates independently of engine releases.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class HumanizationMarkers(BaseModel):
    """Compiled marker lists used by the scorer (H2), editor (H3), and
    implied-claim checker (H4)."""

    suppressed_vocabulary: list[str] = Field(default_factory=list)
    hedging_phrases: list[str] = Field(default_factory=list)
    formulaic_transitions: list[str] = Field(default_factory=list)
    contrastive_patterns: list[str] = Field(
        default_factory=list,
        description="Raw regex strings; compiled by callers via compiled_contrastive_patterns().",
    )

    model_config = {"frozen": True}

    def compiled_contrastive_patterns(self) -> list[re.Pattern[str]]:
        """Compile contrastive regex patterns once for repeated scoring."""
        return [re.compile(p, re.IGNORECASE) for p in self.contrastive_patterns]


def load_markers(path: str | Path) -> HumanizationMarkers:
    """Load and validate the markers YAML.

    Raises FileNotFoundError if the path is missing — humanization must not
    silently fall back to an empty marker set when the operator expected one.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Humanization markers file not found: {p}")

    with p.open() as f:
        data = yaml.safe_load(f) or {}

    return HumanizationMarkers(
        suppressed_vocabulary=list(data.get("suppressed_vocabulary", [])),
        hedging_phrases=list(data.get("hedging_phrases", [])),
        formulaic_transitions=list(data.get("formulaic_transitions", [])),
        contrastive_patterns=list(data.get("contrastive_patterns", [])),
    )
