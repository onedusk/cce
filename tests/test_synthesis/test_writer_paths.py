"""Tests for path-aware writer (PathConfig integration)."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.models.paths import PathConfig
from cce.synthesis.writer import WRITER_SYSTEM_PROMPT, Writer
from tests.conftest import MockLLMProvider, make_curation_request, make_evidence


def _make_learn_config(**overrides) -> PathConfig:
    defaults = dict(
        id="learn",
        name="Learn",
        tone="pedagogical",
        structure="essay",
        depth="foundational",
        section_requirements=["overview", "8_dimensions_framing", "closing_frame"],
        max_words=3000,
        prompt_addendum="Write as a foundational essay.",
    )
    defaults.update(overrides)
    return PathConfig(**defaults)


def _make_writer_json() -> str:
    return json.dumps(
        {
            "content": "# Test\nSome content [ev:ev_001].",
            "citations_used": ["ev_001"],
            "evidence_map": [
                {"claim": "Some content", "evidence_ids": ["ev_001"]}
            ],
            "gaps": [],
        }
    )


class TestBuildPathAddendum:
    def test_includes_tone_structure_depth(self):
        pc = _make_learn_config()
        addendum = Writer._build_path_addendum(pc)
        assert "Tone: pedagogical" in addendum
        assert "Structure: essay" in addendum
        assert "Depth: foundational" in addendum

    def test_includes_section_requirements(self):
        pc = _make_learn_config(
            section_requirements=["overview", "closing"]
        )
        addendum = Writer._build_path_addendum(pc)
        assert "Required sections: overview, closing" in addendum

    def test_includes_max_words(self):
        pc = _make_learn_config(max_words=3000)
        addendum = Writer._build_path_addendum(pc)
        assert "Target length: ~3000 words" in addendum

    def test_includes_prompt_addendum(self):
        pc = _make_learn_config(prompt_addendum="Write calmly and clearly.")
        addendum = Writer._build_path_addendum(pc)
        assert "Write calmly and clearly." in addendum

    def test_markers(self):
        pc = _make_learn_config()
        addendum = Writer._build_path_addendum(pc)
        assert "--- PATH-SPECIFIC GUIDANCE" in addendum
        assert "--- END PATH GUIDANCE ---" in addendum

    def test_no_section_requirements(self):
        pc = _make_learn_config(section_requirements=[], max_words=None, prompt_addendum=None)
        addendum = Writer._build_path_addendum(pc)
        assert "Required sections" not in addendum
        assert "Target length" not in addendum


@pytest.mark.integration
class TestWriteWithPathConfig:
    async def test_system_prompt_contains_addendum(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [LLMResponse(content=_make_writer_json(), model="mock", stop_reason="end_turn")]
        )
        writer = Writer(llm)
        pc = _make_learn_config()

        await writer.write(make_curation_request(), [ev], "learn", path_config=pc)

        system = llm.calls[0]["system"]
        assert system.startswith(WRITER_SYSTEM_PROMPT)
        assert "Tone: pedagogical" in system
        assert "--- PATH-SPECIFIC GUIDANCE" in system

    async def test_audience_override(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [LLMResponse(content=_make_writer_json(), model="mock", stop_reason="end_turn")]
        )
        writer = Writer(llm)
        pc = _make_learn_config(audience_override="expert")

        await writer.write(
            make_curation_request(audience="general"), [ev], "learn", path_config=pc
        )

        user_msg = llm.calls[0]["messages"][0].content
        assert "Target audience: expert" in user_msg
        assert "Target audience: general" not in user_msg

    async def test_audience_fallback_to_request(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [LLMResponse(content=_make_writer_json(), model="mock", stop_reason="end_turn")]
        )
        writer = Writer(llm)
        pc = _make_learn_config(audience_override=None)

        await writer.write(
            make_curation_request(audience="general"), [ev], "learn", path_config=pc
        )

        user_msg = llm.calls[0]["messages"][0].content
        assert "Target audience: general" in user_msg
