"""Tests for cce.synthesis.writer — evidence block formatting, response parsing, and prompt construction."""

import json
from unittest.mock import AsyncMock

import pytest

from cce.config.types import WriterConfig
from cce.evidence.formatting import format_evidence_for_prompt
from cce.llm.base import (
    IncompleteResponseError,
    LLMResponse,
    UnparseableResponseError,
)
from cce.models.content import ContentLineage
from cce.models.evidence import SourceQuality
from cce.models.request import CurationConstraints
from cce.synthesis.writer import (
    WRITER_OUTPUT_SCHEMA,
    WRITER_SYSTEM_PROMPT,
    Writer,
    WriterOutput,
)
from tests.conftest import MockLLMProvider, make_curation_request, make_evidence

# ---------------------------------------------------------------------------
# format_evidence_for_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def testformat_evidence_for_prompt_formatting():
    ev1 = make_evidence(
        id="ev_001", url="https://a.com", title="Title A", author="Author A"
    )
    ev2 = make_evidence(
        id="ev_002", url="https://b.com", title="Title B", author="Author B"
    )
    block = format_evidence_for_prompt([ev1, ev2])
    assert "--- EVIDENCE [ev_001] ---" in block
    assert "--- EVIDENCE [ev_002] ---" in block
    assert "URL: https://a.com" in block
    assert "Title: Title A" in block
    assert "Author: Author A" in block
    assert ev1.excerpt in block
    assert ev2.excerpt in block


@pytest.mark.unit
def testformat_evidence_for_prompt_peer_reviewed_tag():
    ev = make_evidence(
        id="ev_pr",
        source_quality=SourceQuality(is_peer_reviewed=True),
    )
    block = format_evidence_for_prompt([ev])
    assert "Type: peer-reviewed" in block


@pytest.mark.unit
def testformat_evidence_for_prompt_primary_source_tag():
    ev = make_evidence(
        id="ev_ps",
        source_quality=SourceQuality(is_primary_source=True),
    )
    block = format_evidence_for_prompt([ev])
    assert "Type: primary-source" in block


@pytest.mark.unit
def testformat_evidence_for_prompt_optional_fields():
    ev = make_evidence(
        id="ev_bare",
        title=None,
        author=None,
        published_at=None,
        source_quality=None,
    )
    block = format_evidence_for_prompt([ev])
    assert "--- EVIDENCE [ev_bare] ---" in block
    assert "URL:" in block
    # Optional fields should not appear
    assert "Title:" not in block
    assert "Author:" not in block
    assert "Published:" not in block
    assert "Reputation:" not in block


# ---------------------------------------------------------------------------
# WRITER_SYSTEM_PROMPT — scaffolding ban (M01)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writer_system_prompt_bans_scaffolding():
    assert "STRUCTURE GUIDANCE" in WRITER_SYSTEM_PROMPT
    assert "In this essay" in WRITER_SYSTEM_PROMPT
    assert "Closing Frame" in WRITER_SYSTEM_PROMPT
    assert "Overview" in WRITER_SYSTEM_PROMPT


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

    def test_parse_response_non_json_raises(self):
        """No raw-markdown fallback: an unreadable reply raises, with the reply
        on the error for the caller and not in the message."""
        ev = make_evidence(id="ev_001")
        response = LLMResponse(
            content="Just plain markdown text", model="mock", stop_reason="end_turn"
        )
        with pytest.raises(UnparseableResponseError) as exc:
            self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert exc.value.raw_response == "Just plain markdown text"
        assert exc.value.role == "writer"
        assert "plain markdown" not in str(exc.value)

    @pytest.mark.parametrize(
        "reply",
        [
            {"content": {"text": "SENTINEL-CONFIDENTIAL"}},
            {"draft": "SENTINEL-CONFIDENTIAL"},
            {"content": "ok", "citations_used": "SENTINEL-CONFIDENTIAL"},
            {"content": "ok", "evidence_map": [{"claim": ["SENTINEL"]}]},
            {"content": "ok", "gaps": [1, 2]},
        ],
        ids=[
            "content-object",
            "no-content",
            "citations-str",
            "claim-list",
            "gaps-ints",
        ],
    )
    def test_parse_response_wrong_shape_raises_without_quoting_reply(self, reply):
        """Wrongly typed fields raise UnparseableResponseError before any model
        is built, so no ValidationError quotes the reply into logs or the job
        record."""
        ev = make_evidence(id="ev_001")
        response = LLMResponse(content=json.dumps(reply), model="m")
        with pytest.raises(UnparseableResponseError) as exc:
            self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert "SENTINEL" not in str(exc.value)

    def test_parse_response_empty_content_is_not_an_error(self):
        """An empty draft is a legitimate "no content" outcome (routes to
        review), not an unreadable reply."""
        ev = make_evidence(id="ev_001")
        response = LLMResponse(content=_make_writer_json(content=""), model="m")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert output.has_content is False

    def test_parse_response_unknown_citation_ids_filtered(self):
        ev = make_evidence(id="ev_001")
        raw = _make_writer_json(citations_used=["ev_001", "ev_unknown"])
        response = LLMResponse(content=raw, model="mock")
        output = self._writer()._parse_response(response, [ev], "blog", self._lineage())

        assert len(output.unit.citations) == 1
        assert output.unit.citations[0].evidence_id == "ev_001"

    def test_parse_response_unknown_citation_id_is_clipped_in_log(self, caplog):
        """A model-supplied ID can't smuggle reply text or a forged line into logs."""
        smuggled = "ev_" + "A" * 30 + "\nINJECTED log line " + "B" * 100
        raw = _make_writer_json(citations_used=["ev_001", smuggled])
        response = LLMResponse(content=raw, model="mock")

        with caplog.at_level("WARNING"):
            self._writer()._parse_response(
                response, [make_evidence(id="ev_001")], "blog", self._lineage()
            )

        (record,) = [r for r in caplog.records if "unknown evidence ID" in r.message]
        assert "\n" not in record.message
        assert "INJECTED" not in record.message
        assert "BBBB" not in record.message
        assert repr(smuggled[:40]) in record.message

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
        ev1 = make_evidence(
            id="ev_001", url="https://a.com", excerpt="Excerpt A is long enough."
        )
        ev2 = make_evidence(
            id="ev_002", url="https://b.com", excerpt="Excerpt B is long enough."
        )
        ev3 = make_evidence(
            id="ev_003", url="https://c.com", excerpt="Excerpt C is long enough."
        )
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
async def test_write_with_sibling_context_injects_block():
    """M03: sibling_context produces the prose-level de-dup block, including the
    'not on citations' clause that protects the citation invariant (ADR-003)."""
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)

    await writer.write(
        make_curation_request(),
        [ev],
        "explore",
        sibling_context="## From the 'learn' article:\n- Loneliness affects 16% of people",
    )

    user_msg = llm.calls[0]["messages"][0].content
    assert "ALREADY COVERED BY SIBLING ARTICLES" in user_msg
    assert "From the 'learn' article" in user_msg
    assert "Loneliness affects 16% of people" in user_msg
    # The clause that keeps de-dup prose-level, not citation-level.
    assert "not on citations" in user_msg


@pytest.mark.integration
async def test_write_without_sibling_context_omits_block():
    """sibling_context=None (the default / first path) omits the block entirely."""
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)

    await writer.write(make_curation_request(), [ev], "learn")

    user_msg = llm.calls[0]["messages"][0].content
    assert "ALREADY COVERED BY SIBLING ARTICLES" not in user_msg
    assert "SIBLING CONTEXT" not in user_msg


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


@pytest.mark.integration
async def test_write_temperature_from_config():
    """B1: the writer's temperature is a WriterConfig default, not a literal."""
    ev = make_evidence(id="ev_001")
    llm = MockLLMProvider(
        [LLMResponse(content=_make_writer_json(), model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm, WriterConfig(temperature=0.5))

    await writer.write(make_curation_request(), [ev], "blog")

    assert llm.calls[0]["temperature"] == 0.5


@pytest.mark.integration
async def test_write_includes_jurisdiction_in_prompt():
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)
    request = make_curation_request(
        constraints=CurationConstraints(jurisdiction="US"),
    )

    await writer.write(request, [ev], "blog")

    user_msg = llm.calls[0]["messages"][0].content
    assert "Jurisdiction/scope: US" in user_msg


@pytest.mark.integration
async def test_write_without_path_config_uses_base_prompt():
    """Backward compat: no path_config means system prompt is exactly WRITER_SYSTEM_PROMPT."""
    ev = make_evidence(id="ev_001")
    raw_json = _make_writer_json()
    llm = MockLLMProvider(
        [LLMResponse(content=raw_json, model="mock", stop_reason="end_turn")]
    )
    writer = Writer(llm)

    await writer.write(make_curation_request(), [ev], "blog")

    assert llm.calls[0]["system"] == WRITER_SYSTEM_PROMPT


@pytest.mark.integration
@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
async def test_write_raises_on_incomplete_reply(stop_reason):
    """B2: a truncated/refused reply raises instead of degrading into the
    uncited raw-markdown fallback, and is not resent (one call only)."""
    llm = MockLLMProvider(
        [
            LLMResponse(
                content='{"content": "Partial draft [ev:ev_001] and then',
                model="claude-sonnet-5",
                usage={"output_tokens": 16384},
                stop_reason=stop_reason,
            )
        ]
    )
    writer = Writer(llm)

    with pytest.raises(IncompleteResponseError, match="writer"):
        await writer.write(
            make_curation_request(), [make_evidence(id="ev_001")], "blog"
        )

    assert len(llm.calls) == 1


@pytest.mark.integration
async def test_write_passes_structured_output_schema():
    llm = MockLLMProvider(
        [LLMResponse(content=_make_writer_json(), model="mock", stop_reason="end_turn")]
    )

    await Writer(llm).write(
        make_curation_request(), [make_evidence(id="ev_001")], "blog"
    )

    assert llm.calls[0]["output_schema"] == WRITER_OUTPUT_SCHEMA


@pytest.mark.integration
async def test_write_resends_once_on_unparseable_reply(monkeypatch):
    monkeypatch.setattr("cce.llm.retry.asyncio.sleep", AsyncMock())
    llm = MockLLMProvider(
        [
            LLMResponse(content="not json", model="mock", stop_reason="end_turn"),
            LLMResponse(
                content=_make_writer_json(), model="mock", stop_reason="end_turn"
            ),
        ]
    )

    output = await Writer(llm).write(
        make_curation_request(), [make_evidence(id="ev_001")], "blog"
    )

    assert len(llm.calls) == 2
    assert output.has_content


@pytest.mark.integration
async def test_write_token_usage_includes_the_discarded_attempt(monkeypatch):
    monkeypatch.setattr("cce.llm.retry.asyncio.sleep", AsyncMock())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="not json",
                model="mock",
                usage={"input_tokens": 100, "output_tokens": 7},
                stop_reason="end_turn",
            ),
            LLMResponse(
                content=_make_writer_json(),
                model="mock",
                usage={"input_tokens": 120, "output_tokens": 30},
                stop_reason="end_turn",
            ),
        ]
    )

    output = await Writer(llm).write(
        make_curation_request(), [make_evidence(id="ev_001")], "blog"
    )

    assert output.token_usage == {"input_tokens": 220, "output_tokens": 37}


@pytest.mark.integration
async def test_write_raises_after_second_unparseable_reply(monkeypatch):
    """Retry once, then fail: no raw-markdown fallback, no third call."""
    monkeypatch.setattr("cce.llm.retry.asyncio.sleep", AsyncMock())
    llm = MockLLMProvider(
        [
            LLMResponse(content="first bad", model="m", stop_reason="end_turn"),
            LLMResponse(content="second bad", model="m", stop_reason="end_turn"),
        ]
    )

    with pytest.raises(UnparseableResponseError) as exc:
        await Writer(llm).write(
            make_curation_request(), [make_evidence(id="ev_001")], "blog"
        )

    assert len(llm.calls) == 2
    assert exc.value.raw_response == "second bad"
    # The first attempt is chained, so a caller can reach both replies.
    assert isinstance(exc.value.__cause__, UnparseableResponseError)
    assert exc.value.__cause__.raw_response == "first bad"


@pytest.mark.unit
async def test_citation_ids_written_in_marker_form_are_kept():
    """Haiku 4.5 lists citations as "ev:<hash>" (the marker syntax without
    the ev_ prefix); they used to be dropped, leaving unit.citations empty."""
    import json

    from cce.llm.base import LLMResponse
    from cce.synthesis.writer import Writer
    from tests.conftest import MockLLMProvider, make_curation_request, make_evidence

    ev = make_evidence(id="ev_bbac54615a34")
    reply = json.dumps(
        {
            "content": "A claim [ev:bbac54615a34].",
            "citations_used": ["ev:bbac54615a34"],
            "evidence_map": [{"claim": "A claim", "evidence_ids": ["ev:bbac54615a34"]}],
            "gaps": [],
        }
    )
    llm = MockLLMProvider([LLMResponse(content=reply, model="m")])
    out = await Writer(llm).write(make_curation_request(), [ev], "blog")

    assert [c.evidence_id for c in out.unit.citations] == ["ev_bbac54615a34"]
    assert out.unit.evidence_map[0].evidence_ids == ["ev_bbac54615a34"]
    assert out.unit.scores.source_diversity == 1.0
