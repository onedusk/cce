"""Loader for the humanization markers YAML.

Kept separate from config/types.py so the marker file can grow large without
bloating the typed-config module. Loaded once at engine construction; the
coevolution of AI markers (documented in docs/internal/research/) means this
list updates independently of engine releases.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ContrastiveSubtype = Literal["parasitic", "genuine_alternative"]


class HumanizationMarkers(BaseModel):
    """Compiled marker lists used by the scorer (H2), editor (H3), and
    implied-claim checker (H4)."""

    suppressed_vocabulary: list[str] = Field(default_factory=list)
    hedging_phrases: list[str] = Field(default_factory=list)
    formulaic_transitions: list[str] = Field(default_factory=list)
    contrastive_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Genuine-alternative contrastive regexes — Y is a real "
            "alternative to X with its own evidence potential (e.g. "
            "'Unlike X, Y' / 'rather than X'). Existing shape preserved "
            "for backward compatibility; loader keeps this key's contents "
            "tagged as genuine_alternative."
        ),
    )
    contrastive_parasitic_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Parasitic contrastive regexes — Y is a degraded/reframed form "
            "of A rather than an independent alternative (e.g. 'X is not A. "
            "It is B'). Editor collapses these; ImpliedClaimChecker skips "
            "the LLM topic-extraction call since there is no dismissed "
            "topic with its own literature."
        ),
    )

    model_config = {"frozen": True}

    def compiled_contrastive_patterns(
        self,
    ) -> list[tuple[re.Pattern[str], ContrastiveSubtype]]:
        """Compile contrastive regexes with their subtype tags.

        Genuine-alternative patterns (``contrastive_patterns``) come first so
        downstream deterministic iteration order matches the pre-refactor
        behavior for the overlap of regexes that existed before.
        """
        out: list[tuple[re.Pattern[str], ContrastiveSubtype]] = []
        for p in self.contrastive_patterns:
            out.append((re.compile(p, re.IGNORECASE), "genuine_alternative"))
        for p in self.contrastive_parasitic_patterns:
            out.append((re.compile(p, re.IGNORECASE), "parasitic"))
        return out


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
        contrastive_parasitic_patterns=list(
            data.get("contrastive_parasitic_patterns", [])
        ),
    )
