"""Tests for taxonomy data models (Dimension, TaxonomyConfig)."""

import pytest

from cce.models.taxonomy import Dimension, TaxonomyConfig


def _make_dimension(**overrides) -> Dimension:
    defaults = {
        "id": "emotional",
        "name": "Emotional Well-Being",
        "description": "How we feel",
        "values": ["primary", "secondary", "none"],
    }
    defaults.update(overrides)
    return Dimension(**defaults)


def _make_taxonomy(**overrides) -> TaxonomyConfig:
    dims = overrides.pop("dimensions", None) or [
        _make_dimension(id="emotional", name="Emotional"),
        _make_dimension(id="physical", name="Physical"),
    ]
    defaults = {
        "id": "test-taxonomy",
        "name": "Test Taxonomy",
        "dimensions": dims,
    }
    defaults.update(overrides)
    return TaxonomyConfig(**defaults)


class TestDimension:
    def test_construction(self):
        d = _make_dimension()
        assert d.id == "emotional"
        assert d.name == "Emotional Well-Being"
        assert d.description == "How we feel"
        assert d.values == ["primary", "secondary", "none"]

    def test_description_optional(self):
        d = _make_dimension(description=None)
        assert d.description is None

    def test_frozen(self):
        d = _make_dimension()
        with pytest.raises(Exception):
            d.id = "changed"


class TestTaxonomyConfig:
    def test_construction(self):
        tc = _make_taxonomy()
        assert tc.id == "test-taxonomy"
        assert tc.name == "Test Taxonomy"
        assert len(tc.dimensions) == 2

    def test_frozen(self):
        tc = _make_taxonomy()
        with pytest.raises(Exception):
            tc.id = "changed"

    def test_dimension_ids(self):
        tc = _make_taxonomy()
        assert tc.dimension_ids() == ["emotional", "physical"]

    def test_get_dimension_found(self):
        tc = _make_taxonomy()
        d = tc.get_dimension("emotional")
        assert d is not None
        assert d.name == "Emotional"

    def test_get_dimension_not_found(self):
        tc = _make_taxonomy()
        assert tc.get_dimension("nonexistent") is None
