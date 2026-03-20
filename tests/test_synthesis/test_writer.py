"""Tests for cce.synthesis.writer — evidence block formatting, response parsing, and prompt construction."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.models.content import ContentLineage
from cce.synthesis.writer import WRITER_SYSTEM_PROMPT, Writer, WriterOutput, _build_evidence_block
from tests.conftest import MockLLMProvider, make_curation_request, make_evidence


# ---------------------------------------------------------------------------
# _build_evidence_block
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_evidence_block_formatting():
    ev1 = make_evidence(id="ev_001", url="https://a.com", title="Title A", author="Author A")
    ev2 = make_evidence(id="ev_002", url="https://b.com", title="Title B", author="Author B")
    block = _build_evidence_block([ev1, ev2])
    assert "--- EVIDENCE [ev_001] ---" in block
    assert "--- EVIDENCE [ev_002] ---" in block
    assert "URL: https://a.com" in block
    assert "Title: Title A" in block
    assert "Author: Author A" in block
    assert ev1.excerpt in block
    assert ev2.excerpt in block


@pytest.mark.unit
def test_build_evidence_block_optional_fields():
    ev = make_evidence(
        id="ev_bare",
        title=None,
        author=None,
        published_at=None,
        source_quality=None,
    )
    block = _build_evidence_block([ev])
    assert "--- EVIDENCE [ev_bare] ---" in block
    assert "URL:" in block
    # Optional fields should not appear
    assert "Title:" not in block
    assert "Author:" not in block
    assert "Published:" not in block
    assert "Reputation:" not in block


# ---------------------------------------------------------------------------
# Writer.write — early return
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_writer_no_evidence():
    llm = MockLLMProvider([])
    writer = Writer(llm)
    request = make_curation_request()
    output = await writer.write(request, evidence=[], path="blog")
    assert output.unit is None
    assert output.has_content is False
    assert len(output.gaps) > 0
    assert len(llm.calls) == 0  # no LLM call made


# ---------------------------------------------------------------------------
# Writer._parse_response — unit tests (sync)
# ---------------------------------------------------------------------------


def _make_writer_json(
    *,
    content: str = "Draft content [ev:ev_001].",
    citations_used: list[str] | None = None,
    evidence_map: list[dict] | None = None,
    gaps: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "content": content,
            "citations_used": ["ev_001"] if citations_used is None else citations_used,
            "evidence_map": [{"claim": "Draft content", "evidence_ids": ["ev_001"]}]
            if evidence_map is None
            else evidence_map,
            "gaps": [] if gaps is None else gaps,
        }
    )


class TestParseResponse:
    pytestmark = pytest.mark.unit

    def _writer(self) -> Writer:
        return Writer(MockLLMProvider([]))

    def _lineage(self) -> ContentLineage:
        return ContentLineage(policy_id="p", run_id="r", engine_version="0.1.0")

    def test_parse_response_valid_json(self):
        ev = make_evidence(id="ev_001", url="https://example.com/a")
        raw_json = _make_writer_json()
        response = LLMResponse(content=raw_json, model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert output.unit is not None
        assert "Draft content" in output.unit.content
        assert len(output.unit.citations) == 1
        assert output.unit.citations[0].evidence_id == "ev_001"
        assert output.unit.citations[0].url == "https://example.com/a"
        assert len(output.unit.evidence_map) == 1
        assert output.unit.evidence_map[0].claim == "Draft content"

    def test_parse_response_non_json_fallback(self):
        ev = make_evidence(id="ev_001")
        response = LLMResponse(content="Just plain markdown text", model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert output.unit is not None
        assert output.unit.content == "Just plain markdown text"
        assert output.unit.citations == []
        assert output.unit.evidence_map == []
        assert output.unit.scores.confidence == 0.0

    def test_parse_response_unknown_citation_ids_filtered(self):
        ev = make_evidence(id="ev_001")
        raw = _make_writer_json(citations_used=["ev_001", "ev_unknown"])
        response = LLMResponse(content=raw, model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert len(output.unit.citations) == 1
        assert output.unit.citations[0].evidence_id == "ev_001"

    def test_parse_response_empty_claims_filtered(self):
        ev = make_evidence(id="ev_001")
        raw = _make_writer_json(
            evidence_map=[
                {"claim": "", "evidence_ids": ["ev_001"]},
                {"claim": "Valid claim", "evidence_ids": ["ev_001"]},
            ]
        )
        response = LLMResponse(content=raw, model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert len(output.unit.evidence_map) == 1
        assert output.unit.evidence_map[0].claim == "Valid claim"

    def test_parse_response_diversity_calculation(self):
        ev1 = make_evidence(id="ev_001", url="https://a.com", excerpt="Excerpt A is long enough.")
        ev2 = make_evidence(id="ev_002", url="https://b.com", excerpt="Excerpt B is long enough.")
        ev3 = make_evidence(id="ev_003", url="https://c.com", excerpt="Excerpt C is long enough.")
        # LLM cites 2 of 3 sources
        raw = _make_writer_json(citations_used=["ev_001", "ev_002"])
        response = LLMResponse(content=raw, model="mock")
        output = self._writer()._parse_response(
            response, [ev1, ev2, ev3], "blog", self._lineage()
        )

        assert output.unit.scores.source_diversity == pytest.approx(2 / 3, abs=0.01)

    def test_parse_response_diversity_zero(self):
        ev = make_evidence(id="ev_001")
        raw = _make_writer_json(citations_used=[])
        response = LLMResponse(content=raw, model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert output.unit.scores.source_diversity == 0.0


# ---------------------------------------------------------------------------
# WriterOutput properties
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writer_output_properties():
    from cce.models.content import ContentScores, ContentUnit

    unit = ContentUnit(
        id="cu_test",
        path="blog",
        content="Some content",
        scores=ContentScores(confidence=0.0, coverage=0.0, source_diversity=0.0),
        lineage=ContentLineage(policy_id="p", run_id="r", engine_version="0.1.0"),
    )
    output_with = WriterOutput(unit=unit, gaps=[], raw_response="")
    assert output_with.has_content is True
    assert output_with.has_gaps is False

    output_without = WriterOutput(unit=None, gaps=["gap"], raw_response="")
    assert output_without.has_content is False
    assert output_without.has_gaps is True


# ---------------------------------------------------------------------------
# Writer.write — integration tests (async, mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_sends_correct_prompt():
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)
    request = make_curation_request(topic="test topic", subtopics=["sub1"])

    await writer.write(request, [ev], "blog")

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["system"] == WRITER_SYSTEM_PROMPT
    user_msg = call["messages"][0].content
    assert "test topic" in user_msg
    assert "sub1" in user_msg
    assert "blog" in user_msg
    assert f"--- EVIDENCE [{ev.id}] ---" in user_msg
    assert "1 evidence" in user_msg


@pytest.mark.integration
async def test_write_with_feedback():
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)
    request = make_curation_request()

    await writer.write(request, [ev], "blog", feedback="Fix claim X")

    user_msg = llm.calls[0]["messages"][0].content
    assert "VERIFIER FEEDBACK" in user_msg
    assert "Fix claim X" in user_msg


@pytest.mark.integration
async def test_write_temperature():
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)

    await writer.write(make_curation_request(), [ev], "blog")

    assert llm.calls[0]["temperature"] == 0.2
