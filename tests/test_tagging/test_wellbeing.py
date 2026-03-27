"""Tests for WellBeingTaxonomy rules-based classifier."""

from __future__ import annotations

import pytest

from cce.tagging.base import TaxonomyPlugin
from cce.tagging.wellbeing import WellBeingTaxonomy
from tests.conftest import make_evidence


class TestWellBeingTaxonomy:
    def test_satisfies_protocol(self, wellbeing_taxonomy):
        assert isinstance(wellbeing_taxonomy, TaxonomyPlugin)

    async def test_emotional_keywords(self, wellbeing_taxonomy):
        ev = make_evidence(
            excerpt=(
                "Anxiety and stress cause emotional dysregulation. "
                "Mood instability and irritability are common."
            ),
        )
        result = await wellbeing_taxonomy.tag(ev)
        assert "emotional" in result.tags
        assert result.signals["emotional"] in ("primary", "secondary")

    async def test_intellectual_keywords(self, wellbeing_taxonomy):
        ev = make_evidence(
            excerpt=(
                "Cognitive function depends on attention, focus, and memory. "
                "Learning and decision-making improve with practice."
            ),
        )
        result = await wellbeing_taxonomy.tag(ev)
        assert "intellectual" in result.tags
        assert result.signals["intellectual"] == "primary"

    async def test_physical_keywords(self, wellbeing_taxonomy):
        ev = make_evidence(
            excerpt=(
                "Sleep quality affects physical health and body recovery. "
                "Exercise reduces fatigue and pain over time."
            ),
        )
        result = await wellbeing_taxonomy.tag(ev)
        assert "physical" in result.tags

    async def test_financial_keywords(self, wellbeing_taxonomy, financial_evidence):
        result = await wellbeing_taxonomy.tag(financial_evidence)
        assert "financial" in result.tags
        assert result.signals["financial"] in ("primary", "secondary")

    async def test_multi_dimension(self, wellbeing_taxonomy):
        """Evidence touching multiple dimensions gets multiple tags."""
        ev = make_evidence(
            excerpt=(
                "Work stress and burnout affect emotional wellbeing. "
                "Career anxiety leads to mood instability and irritability."
            ),
        )
        result = await wellbeing_taxonomy.tag(ev)
        assert len(result.tags) >= 2
        tagged_dims = set(result.tags)
        # Should pick up both vocational and emotional
        assert "vocational" in tagged_dims or "emotional" in tagged_dims

    async def test_no_match(self, wellbeing_taxonomy, neutral_evidence):
        result = await wellbeing_taxonomy.tag(neutral_evidence)
        assert result.tags == []
        assert result.confidence == 0.0

    async def test_confidence_scaling(self, wellbeing_taxonomy):
        """More keyword matches = higher confidence, capped at 1.0."""
        # Few matches
        ev_few = make_evidence(excerpt="Sleep is important.")
        r_few = await wellbeing_taxonomy.tag(ev_few)

        # Many matches
        ev_many = make_evidence(
            excerpt=(
                "Sleep quality, exercise routines, physical health, body recovery, "
                "movement patterns, fatigue management, pain reduction, and health outcomes."
            ),
        )
        r_many = await wellbeing_taxonomy.tag(ev_many)

        assert r_many.confidence > r_few.confidence
        assert r_many.confidence <= 1.0

    async def test_tag_many_ordering(self, wellbeing_taxonomy):
        ev1 = make_evidence(id="ev_1", excerpt="Financial budgeting and debt management.")
        ev2 = make_evidence(id="ev_2", excerpt="Sleep and exercise improve health.")
        results = await wellbeing_taxonomy.tag_many([ev1, ev2])
        assert len(results) == 2
        assert "financial" in results[0].tags
        assert "physical" in results[1].tags

    async def test_case_insensitive(self, wellbeing_taxonomy):
        ev = make_evidence(excerpt="ANXIETY and STRESS cause EMOTIONAL problems.")
        result = await wellbeing_taxonomy.tag(ev)
        assert "emotional" in result.tags
