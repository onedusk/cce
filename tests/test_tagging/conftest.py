"""Shared fixtures for tagging tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cce.models.taxonomy import TaxonomyConfig
from cce.tagging.loader import load_taxonomy
from cce.tagging.wellbeing import WellBeingTaxonomy

# Resolve taxonomy YAML relative to project root
_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "taxonomies" / "wellbeing-8d.yaml"

# Re-use the global make_evidence factory from tests/conftest.py
from tests.conftest import make_evidence  # noqa: E402


@pytest.fixture
def wellbeing_config() -> TaxonomyConfig:
    return load_taxonomy(_TAXONOMY_PATH)


@pytest.fixture
def wellbeing_taxonomy(wellbeing_config: TaxonomyConfig) -> WellBeingTaxonomy:
    return WellBeingTaxonomy(wellbeing_config)


@pytest.fixture
def emotional_evidence():
    return make_evidence(
        excerpt=(
            "Anxiety and stress cause emotional dysregulation. "
            "Mood instability and irritability are common symptoms. "
            "Emotional wellbeing requires resilience and self-awareness."
        ),
    )


@pytest.fixture
def financial_evidence():
    return make_evidence(
        excerpt=(
            "Financial budgeting and investment strategies are key to economic stability. "
            "Managing debt, income, and savings requires discipline."
        ),
    )


@pytest.fixture
def neutral_evidence():
    return make_evidence(
        excerpt="The quick brown fox jumps over the lazy dog.",
    )
