"""Tests for PathConfig data model."""

import pytest
from pydantic import ValidationError

from cce.models.paths import PathConfig


class TestPathConfig:
    def test_defaults(self):
        pc = PathConfig(id="learn", name="Learn")
        assert pc.tone == "neutral"
        assert pc.structure == "essay"
        assert pc.depth == "foundational"
        assert pc.description is None
        assert pc.audience_override is None
        assert pc.section_requirements == []
        assert pc.max_words is None
        assert pc.prompt_addendum is None
        assert pc.max_evidence is None
        assert pc.max_paragraphs is None
        assert pc.subtopic_limit is None

    def test_all_fields(self):
        pc = PathConfig(
            id="learn",
            name="Learn",
            description="Foundational essays",
            tone="pedagogical",
            structure="reference",
            depth="contextual",
            audience_override="expert",
            section_requirements=["overview", "closing"],
            max_words=3000,
            prompt_addendum="Write calmly.",
        )
        assert pc.id == "learn"
        assert pc.tone == "pedagogical"
        assert pc.section_requirements == ["overview", "closing"]
        assert pc.max_words == 3000
        assert pc.prompt_addendum == "Write calmly."

    def test_frozen(self):
        pc = PathConfig(id="learn", name="Learn")
        with pytest.raises(ValidationError):
            pc.id = "changed"

    def test_section_requirements_default_factory(self):
        """Each instance gets its own list (not shared reference)."""
        pc1 = PathConfig(id="a", name="A")
        pc2 = PathConfig(id="b", name="B")
        assert pc1.section_requirements is not pc2.section_requirements

    def test_tuning_fields(self):
        pc = PathConfig(
            id="learn",
            name="Learn",
            max_evidence=30,
            max_paragraphs=10,
            subtopic_limit=2,
        )
        assert pc.max_evidence == 30
        assert pc.max_paragraphs == 10
        assert pc.subtopic_limit == 2
