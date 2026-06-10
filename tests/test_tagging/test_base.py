"""Tests for TaxonomyPlugin protocol and TaggingResult."""

import pytest

from cce.models.evidence import Evidence
from cce.tagging.base import TaggingResult, TaxonomyPlugin, TaxonomyUnavailableError

pytestmark = pytest.mark.unit


class TestTaggingResult:
    def test_construction(self):
        r = TaggingResult(
            tags=["emotional", "physical"],
            signals={"emotional": "primary", "physical": "secondary"},
            confidence=0.7,
        )
        assert r.tags == ["emotional", "physical"]
        assert r.signals["emotional"] == "primary"
        assert r.confidence == 0.7

    def test_defaults(self):
        r = TaggingResult()
        assert r.tags == []
        assert r.signals == {}
        assert r.confidence == 0.0

    def test_frozen(self):
        r = TaggingResult(tags=["a"], confidence=0.5)
        with pytest.raises(AttributeError):
            r.confidence = 0.9


class TestTaxonomyUnavailableError:
    def test_is_exception(self):
        assert issubclass(TaxonomyUnavailableError, Exception)

    def test_message(self):
        err = TaxonomyUnavailableError("service down")
        assert str(err) == "service down"


class TestTaxonomyPluginProtocol:
    def test_isinstance_check(self):
        """A class implementing tag() and tag_many() satisfies the protocol."""

        class MockTaxonomy:
            async def tag(self, evidence: Evidence) -> TaggingResult:
                return TaggingResult()

            async def tag_many(self, evidence: list[Evidence]) -> list[TaggingResult]:
                return []

        assert isinstance(MockTaxonomy(), TaxonomyPlugin)

    def test_isinstance_fails_without_methods(self):
        """A class missing methods does NOT satisfy the protocol."""

        class NotATaxonomy:
            pass

        assert not isinstance(NotATaxonomy(), TaxonomyPlugin)
