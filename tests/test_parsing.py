"""Tests for cce.parsing — JSON extraction and repair from LLM output."""

import pytest

from cce.parsing import EV_MARKER_RE, _repair_json, extract_json, resolve_evidence_id
from tests.conftest import make_evidence

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# extract_json — direct parse
# ---------------------------------------------------------------------------


def test_extract_json_direct_parse():
    assert extract_json('{"key": "value"}') == {"key": "value"}


def test_extract_json_code_fence_json():
    text = '```json\n{"key": "value"}\n```'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_code_fence_plain():
    text = '```\n{"key": "value"}\n```'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_preamble_and_postamble():
    text = 'Here is the result:\n{"key": "value"}\nLet me know if you need changes.'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_nested_braces():
    text = 'Result: {"outer": {"inner": {"deep": 1}}, "list": [1, 2]}'
    result = extract_json(text)
    assert result is not None
    assert result["outer"]["inner"]["deep"] == 1
    assert result["list"] == [1, 2]


def test_extract_json_returns_none_on_garbage():
    assert extract_json("not json at all, just plain text") is None


def test_extract_json_empty_string():
    assert extract_json("") is None


def test_extract_json_multiple_json_blocks():
    # Greedy regex captures from first opening fence to last closing fence,
    # so multiple code-fenced blocks result in None (the combined content
    # isn't valid JSON). This documents actual behavior.
    text = '```json\n{"first": true}\n```\nAnd also:\n```json\n{"second": true}\n```'
    assert extract_json(text) is None

    # But if only one block is fenced, surrounding text is fine
    text2 = 'Here is the answer:\n```json\n{"first": true}\n```\nDone.'
    assert extract_json(text2) == {"first": True}


# ---------------------------------------------------------------------------
# _repair_json — unescaped quote handling
# ---------------------------------------------------------------------------


def test_repair_json_unescaped_quotes():
    # Inner quotes around "lost" are not escaped
    broken = '{"text": "the word "lost" was used"}'
    result = _repair_json(broken)
    assert result is not None
    assert "lost" in result["text"]


def test_repair_json_multiple_unescaped():
    broken = '{"text": "she said "hello" and he said "goodbye" to them"}'
    result = _repair_json(broken)
    assert result is not None
    assert "hello" in result["text"]
    assert "goodbye" in result["text"]


def test_repair_json_returns_none_on_hopeless():
    assert _repair_json("{{{{not json at all}}}}") is None


def test_repair_json_max_repairs_limit():
    # Build a string with way more than 50 unescaped quotes
    inner = " ".join(f'"word{i}"' for i in range(60))
    broken = '{"text": "' + inner + '"}'
    result = _repair_json(broken, max_repairs=50)
    # Should give up after 50 attempts, returning None
    assert result is None


# ---------------------------------------------------------------------------
# extract_json — repair integration and edge cases
# ---------------------------------------------------------------------------


def test_extract_json_triggers_repair():
    # Code fence content has unescaped quotes — should fall through to repair
    text = '```json\n{"claim": "the term "evidence" is key"}\n```'
    result = extract_json(text)
    assert result is not None
    assert "evidence" in result["claim"]


def test_extract_json_crlf_normalization():
    text = '{\r\n"key": "val"\r\n}'
    assert extract_json(text) == {"key": "val"}


# ---------------------------------------------------------------------------
# Citation-marker grammar (B4 — shared by the gate and MDX emit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ev_marker_re_matches_both_writer_formats():
    content = "a [ev:ev_1] b [ev_2] c [not_a_marker] d [ev:3]"
    ids = [m.group(1) or m.group(2) for m in EV_MARKER_RE.finditer(content)]
    assert ids == ["ev_1", "ev_2", "3"]


@pytest.mark.unit
def test_resolve_evidence_id_literal_then_prefixed():
    ev = make_evidence(id="ev_abc")
    by_id = {"ev_abc": ev}

    assert resolve_evidence_id("ev_abc", by_id) == ("ev_abc", ev)
    assert resolve_evidence_id("abc", by_id) == ("ev_abc", ev)
    assert resolve_evidence_id("missing", by_id) == ("missing", None)


@pytest.mark.unit
def test_extract_json_accepts_raw_newline_inside_string():
    """Root cause of the Sonnet 5 writer parse failures (3 of 6 replies,
    2026-09-23): a raw newline inside a long string value instead of the \\n
    escape. Strict parsing rejected it as an invalid control character."""
    raw = '{\n  "content": "Para one [ev_1].\n\nPara two [ev_2].",\n  "gaps": []\n}'
    # The newlines inside the "content" value are real control characters.
    assert "[ev_1].\n\nPara two" in raw

    parsed = extract_json(raw)

    assert parsed is not None
    assert parsed["content"] == "Para one [ev_1].\n\nPara two [ev_2]."


@pytest.mark.unit
def test_extract_json_failure_log_holds_no_reply_text(caplog):
    """Replies can hold confidential content: the failure warning logs the
    length only."""
    with caplog.at_level("WARNING"):
        assert extract_json("SENTINEL-CONFIDENTIAL not json at all") is None

    assert "extract_json failed" in caplog.text
    assert "SENTINEL-CONFIDENTIAL" not in caplog.text
