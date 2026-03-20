"""Tests for cce.parsing — JSON extraction and repair from LLM output."""

import pytest

from cce.parsing import _repair_json, extract_json

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
