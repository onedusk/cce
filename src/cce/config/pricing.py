"""Model pricing for job cost estimates (B15).

A table of USD-per-million-token prices keyed by model ID: input, output,
cache write and cache read. The packaged ``model_pricing.yaml`` holds list
prices; an operator file overrides or extends it (see ``ConfigRegistry``).
Prices change, so the table is data, not code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_SNAPSHOT_DATE_RE = re.compile(r"-\d{8}$")
_PER_TOKEN = 1_000_000


class ModelPricing(BaseModel):
    """One model's prices in USD per million tokens."""

    input: float = Field(ge=0, description="Uncached input tokens")
    output: float = Field(ge=0, description="Output tokens, thinking included")
    cache_write: float = Field(ge=0, description="Tokens written to the prompt cache")
    cache_read: float = Field(ge=0, description="Tokens read from the prompt cache")

    model_config = {"frozen": True, "extra": "forbid"}

    def cost_usd(self, usage: Mapping[str, int]) -> float:
        """Cost of one usage dict (the four keys every LLMResponse reports)."""
        return (
            usage.get("input_tokens", 0) * self.input
            + usage.get("output_tokens", 0) * self.output
            + usage.get("cache_creation_input_tokens", 0) * self.cache_write
            + usage.get("cache_read_input_tokens", 0) * self.cache_read
        ) / _PER_TOKEN


def parse_model_pricing(data: object) -> dict[str, ModelPricing]:
    """``{"models": {id: {input, output, cache_write, cache_read}}}`` -> table."""
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise ValueError("model pricing must be a mapping with a 'models' mapping")
    return {str(model): ModelPricing(**prices) for model, prices in models.items()}


def load_model_pricing(path: Path | None = None) -> dict[str, ModelPricing]:
    """The packaged table, with the entries of ``path`` (when given) on top."""
    packaged = resources.files("cce.config").joinpath("model_pricing.yaml")
    table = parse_model_pricing(yaml.safe_load(packaged.read_text(encoding="utf-8")))
    if path is not None:
        table.update(parse_model_pricing(yaml.safe_load(path.read_text())))
    return table


def price_for(model: str, pricing: Mapping[str, ModelPricing]) -> ModelPricing | None:
    """The entry for ``model``: its own, or the one for the ID without a
    trailing snapshot date (``claude-haiku-4-5-20251001``). No prefix
    guessing: a newer model must not inherit an older one's price."""
    return pricing.get(model) or pricing.get(_SNAPSHOT_DATE_RE.sub("", model))


def estimate_cost_usd(
    usage_by_model: Mapping[str, Mapping[str, int]],
    pricing: Mapping[str, ModelPricing],
) -> float | None:
    """Total cost of a job's usage, or None when any model that was used has
    no price (a partial sum would read as the whole cost)."""
    total = 0.0
    for model, usage in usage_by_model.items():
        price = price_for(model, pricing)
        if price is None:
            return None
        total += price.cost_usd(usage)
    return round(total, 6)
