"""Tests for path-aware writer (PathConfig integration)."""

import json
from pathlib import Path

import pytest

from cce.llm.base import LLMResponse
from cce.models.paths import PathConfig
from cce.synthesis.writer import WRITER_SYSTEM_PROMPT, Writer
from cce.tagging.loader import load_path_configs
from tests.conftest import MockLLMProvider, make_curation_request, make_evidence

_THNKLABS_YAML = Path(__file__).resolve().parents[2] / "path_configs" / "thnklabs.yaml"


def _make_learn_config(**overrides) -> PathConfig:
    defaults = dict(
        id="learn",
        name="Learn",
        tone="pedagogical",
        structure="essay",
        depth="foundational",
        section_requirements=[
            "definition_and_mechanisms",
            "studies_and_evidence",
            "causes_and_risk_factors",
        ],
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
            "evidence_map": [{"claim": "Some content", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


@pytest.mark.unit
class TestBuildPathAddendum:
    def test_includes_tone_structure_depth(self):
        pc = _make_learn_config()
        addendum = Writer._build_path_addendum(pc)
        assert "Tone: pedagogical" in addendum
        assert "Structure: essay" in addendum
        assert "Depth: foundational" in addendum

    def test_includes_section_requirements(self):
        pc = _make_learn_config(section_requirements=["overview", "closing"])
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
        pc = _make_learn_config(
            section_requirements=[], max_words=None, prompt_addendum=None
        )
        addendum = Writer._build_path_addendum(pc)
        assert "Required sections" not in addendum
        assert "Target length" not in addendum

    def test_max_paragraphs_in_addendum(self):
        pc = _make_learn_config(max_paragraphs=10)
        addendum = Writer._build_path_addendum(pc)
        assert "~10 substantive paragraphs" in addendum

    def test_max_paragraphs_absent_when_none(self):
        pc = _make_learn_config(max_paragraphs=None)
        addendum = Writer._build_path_addendum(pc)
        assert "paragraphs" not in addendum


@pytest.mark.integration
class TestWriteWithPathConfig:
    async def test_system_prompt_contains_addendum(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_writer_json(), model="mock", stop_reason="end_turn"
                )
            ]
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
            [
                LLMResponse(
                    content=_make_writer_json(), model="mock", stop_reason="end_turn"
                )
            ]
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
            [
                LLMResponse(
                    content=_make_writer_json(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        writer = Writer(llm)
        pc = _make_learn_config(audience_override=None)

        await writer.write(
            make_curation_request(audience="general"), [ev], "learn", path_config=pc
        )

        user_msg = llm.calls[0]["messages"][0].content
        assert "Target audience: general" in user_msg

    async def test_subtopic_limit(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_writer_json(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        writer = Writer(llm)
        pc = _make_learn_config(subtopic_limit=1)

        await writer.write(
            make_curation_request(subtopics=["sub1", "sub2", "sub3"]),
            [ev],
            "learn",
            path_config=pc,
        )

        user_msg = llm.calls[0]["messages"][0].content
        assert "sub1" in user_msg
        assert "sub2" not in user_msg
        assert "sub3" not in user_msg

    async def test_subtopic_limit_none_uses_all(self):
        ev = make_evidence(id="ev_001")
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_writer_json(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        writer = Writer(llm)
        pc = _make_learn_config(subtopic_limit=None)

        await writer.write(
            make_curation_request(subtopics=["sub1", "sub2"]),
            [ev],
            "learn",
            path_config=pc,
        )

        user_msg = llm.calls[0]["messages"][0].content
        assert "sub1" in user_msg
        assert "sub2" in user_msg


@pytest.mark.unit
@pytest.mark.skipif(
    not _THNKLABS_YAML.exists(),
    reason="path_configs/thnklabs.yaml is gitignored client config (.gitignore '*thnk*'); "
    "absent in fresh clones / CI",
)
def test_thnklabs_paths_mandate_realignment():
    """The real thnklabs.yaml moves the eight-dimensions framing out of LEARN
    into EXPLORE (PDR-001 / ADR-004)."""
    load_path_configs.cache_clear()
    configs = load_path_configs(_THNKLABS_YAML)

    learn = configs["learn"]
    explore = configs["explore"]

    # LEARN no longer carries the eight-dimensions framing.
    assert "8_dimensions_framing" not in learn.section_requirements
    assert "dimensions" not in (learn.prompt_addendum or "").lower()

    # EXPLORE is the new home of the eight-dimensions framing.
    assert "eight_dimensions_framing" in explore.section_requirements
    assert "dimensions" in (explore.prompt_addendum or "").lower()
