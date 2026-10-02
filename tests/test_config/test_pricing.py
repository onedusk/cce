"""Model price table and cost arithmetic (B15)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cce.config.pricing import (
    ModelPricing,
    estimate_cost_usd,
    load_model_pricing,
    price_for,
)
from cce.config.registry import ConfigRegistry
from cce.config.types import EngineConfig, HumanizationConfig, LLMConfig

pytestmark = pytest.mark.unit

MILLION = 1_000_000
FULL = {
    "input_tokens": MILLION,
    "output_tokens": MILLION,
    "cache_creation_input_tokens": MILLION,
    "cache_read_input_tokens": MILLION,
}


def test_packaged_table_prices_the_live_tested_models():
    table = load_model_pricing()

    for model in (
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-opus-5-5",
    ):
        assert model in table
    sonnet = table["claude-sonnet-5"]
    assert (sonnet.input, sonnet.output) == (2.0, 10.0)
    # The 5-minute cache write is 1.25x input on every model.
    assert all(p.cache_write == pytest.approx(p.input * 1.25) for p in table.values())
    assert all(p.cache_read < p.input for p in table.values())


def test_snapshot_dates_match_and_newer_models_do_not_inherit_a_price():
    table = load_model_pricing()

    assert price_for("claude-haiku-4-5-20251001", table) == table["claude-haiku-4-5"]
    assert price_for("claude-opus-5-5", table) != table["claude-opus-5"]
    assert price_for("claude-opus-5-7", table) is None  # no prefix guessing
    assert price_for("mock", table) is None


def test_cost_adds_the_four_token_kinds_per_model():
    table = load_model_pricing()

    assert table["claude-sonnet-5"].cost_usd(FULL) == pytest.approx(14.70)
    total = estimate_cost_usd(
        {
            "claude-sonnet-5": {"input_tokens": 1000, "output_tokens": 2000},
            "claude-haiku-4-5-20251001": {"cache_read_input_tokens": 10_000},
        },
        table,
    )
    assert total == pytest.approx(0.002 + 0.020 + 0.001)


def test_cost_is_none_when_any_model_has_no_price():
    table = load_model_pricing()
    usage = {"claude-sonnet-5": FULL, "some-gateway-model": {"input_tokens": 5}}

    assert estimate_cost_usd(usage, table) is None


def test_override_file_changes_and_adds_entries(tmp_path: Path):
    override = tmp_path / "model_pricing.yaml"
    override.write_text(
        "models:\n"
        "  claude-sonnet-5: {input: 1, output: 2, cache_write: 3, cache_read: 4}\n"
        "  my-gateway-model: {input: 9, output: 9, cache_write: 9, cache_read: 9}\n"
    )
    table = load_model_pricing(override)

    assert table["claude-sonnet-5"].cost_usd(FULL) == pytest.approx(10.0)
    assert "my-gateway-model" in table
    assert table["claude-opus-5"] == load_model_pricing()["claude-opus-5"]


@pytest.mark.parametrize(
    "prices",
    [
        {"input": 1, "output": 1, "cache_write": 1},  # missing key
        {"input": -1, "output": 1, "cache_write": 1, "cache_read": 1},
        {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1, "batch": 1},
    ],
)
def test_malformed_prices_are_rejected(prices):
    with pytest.raises(ValidationError):
        ModelPricing(**prices)


def _registry(root: Path) -> ConfigRegistry:
    engine = EngineConfig(
        llm=LLMConfig(api_key="k"), humanization=HumanizationConfig(enabled=False)
    )
    return ConfigRegistry.load(root, engine=engine)


def test_registry_loads_packaged_prices_and_the_operator_override(tmp_path: Path):
    assert _registry(tmp_path).pricing == load_model_pricing()

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "model_pricing.yaml").write_text(
        "models:\n  my-model: {input: 1, output: 1, cache_write: 1, cache_read: 1}\n"
    )
    assert "my-model" in _registry(tmp_path).pricing


def test_registry_ignores_an_unreadable_override(tmp_path: Path, caplog):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "model_pricing.yaml").write_text("models: [not, a, map]\n")

    assert _registry(tmp_path).pricing == load_model_pricing()
    assert "using the packaged model prices" in caplog.text
