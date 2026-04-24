"""Tests for the Editor agent (humanization M03)."""

from __future__ import annotations

import logging

import pytest

from cce.config.types import EditorConfig
from cce.llm.base import LLMResponse
from cce.models.content import (
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.synthesis.editor import EDITOR_SYSTEM_PROMPT, Editor, EditorOutput
from tests.conftest import MockLLMProvider

pytestmark = pytest.mark.unit


def _make_unit(content: str) -> ContentUnit:
    return ContentUnit(
        id="cu_test",
        path="learn",
        content=content,
        citations=[],
        evidence_map=[],
        scores=ContentScores(confidence=0.0, coverage=0.0, source_diversity=0.0),
        lineage=ContentLineage(policy_id="p", run_id="r", engine_version="0.1.0"),
    )


def _editor(*scripted_responses: str) -> tuple[Editor, MockLLMProvider]:
    llm = MockLLMProvider(
        [LLMResponse(content=s, model="mock") for s in scripted_responses]
    )
    editor = Editor(llm=llm, config=EditorConfig(enabled=True))
    return editor, llm


def _edit_response(content: str, _notes: str = "") -> str:
    """Build a sentinel-delimited editor response for the new output format."""
    return f"=== EDITED START ===\n{content}\n=== EDITED END ==="


def test_editor_system_prompt_contains_hard_constraints():
    """The system prompt must spell out the three behavioral invariants."""
    assert "[ev:EVIDENCE_ID]" in EDITOR_SYSTEM_PROMPT
    assert "MUST appear in your output" in EDITOR_SYSTEM_PROMPT
    assert "MUST NOT introduce" in EDITOR_SYSTEM_PROMPT
    assert "MUST NOT remove or relocate" in EDITOR_SYSTEM_PROMPT


def test_editor_system_prompt_has_parasitic_directive():
    """0.2.0: the system prompt carries separate guidance for parasitic
    contrastive frames (collapse to direct claim) and genuine_alternative
    frames (spectrum principle), with the ambiguous-case caveat."""
    # Genuine-alternative directive preserved
    assert "genuine-alternative contrastive frames" in EDITOR_SYSTEM_PROMPT
    assert "spectrum principle" in EDITOR_SYSTEM_PROMPT
    # Parasitic directive added
    assert "parasitic contrastive frames" in EDITOR_SYSTEM_PROMPT
    assert "collapse to the direct claim" in EDITOR_SYSTEM_PROMPT
    # Anti-attribution guard
    assert "Do not attribute to" in EDITOR_SYSTEM_PROMPT
    # Ambiguous-case caveat (protects the 4.3% class surfaced in Diagnostic 1)
    assert "PRESERVE" in EDITOR_SYSTEM_PROMPT
    assert "clinical" in EDITOR_SYSTEM_PROMPT.lower()


async def test_editor_preserves_all_citations():
    editor, _llm = _editor(
        _edit_response(
            "Sleep fragmentation [ev:abc] changes how we form memories, "
            "and CBT-I [ev:def] targets the habits keeping people awake."
        )
    )
    unit = _make_unit(
        "Sleep fragmentation [ev:abc] affects memory. CBT-I [ev:def] works."
    )

    out = await editor.edit(unit)

    assert out.citations_preserved is True
    assert out.succeeded is True


async def test_editor_flags_dropped_citation(caplog):
    editor, _llm = _editor(
        _edit_response("Sleep fragmentation [ev:abc] affects memory formation.")
        # [ev:def] dropped
    )
    unit = _make_unit(
        "Sleep fragmentation [ev:abc] affects memory. CBT-I [ev:def] works."
    )

    with caplog.at_level(logging.WARNING):
        out = await editor.edit(unit)

    assert out.citations_preserved is False
    assert out.succeeded is False
    assert any("citation drift" in r.message for r in caplog.records)


async def test_editor_flags_added_citation():
    editor, _llm = _editor(
        _edit_response(
            "Sleep fragmentation [ev:abc] affects memory. "
            "Further, stress [ev:xyz] also matters."  # [ev:xyz] not in input
        )
    )
    unit = _make_unit("Sleep fragmentation [ev:abc] affects memory.")

    out = await editor.edit(unit)

    assert out.citations_preserved is False


async def test_editor_records_word_count_metrics():
    editor, _llm = _editor(_edit_response("Short output [ev:abc]."))
    unit = _make_unit("This is a longer original draft with more words in it [ev:abc].")

    out = await editor.edit(unit)

    assert out.word_count_before == 11  # citation marker excluded from the count
    assert out.word_count_after == 2


async def test_editor_succeeded_requires_citations_preserved_and_content():
    out_ok = EditorOutput(
        edited_content="body",
        notes="",
        citations_preserved=True,
        word_count_before=1,
        word_count_after=1,
        raw_response="",
        token_usage={},
    )
    out_empty = EditorOutput(
        edited_content="",
        notes="",
        citations_preserved=True,
        word_count_before=1,
        word_count_after=0,
        raw_response="",
        token_usage={},
    )
    out_drifted = EditorOutput(
        edited_content="body",
        notes="",
        citations_preserved=False,
        word_count_before=1,
        word_count_after=1,
        raw_response="",
        token_usage={},
    )

    assert out_ok.succeeded is True
    assert out_empty.succeeded is False
    assert out_drifted.succeeded is False


async def test_editor_returns_failure_on_missing_sentinels():
    """When the LLM ignores the sentinel format entirely, edited_content
    is empty and the editor reports failure."""
    editor, _llm = _editor("Some prose that ignored the sentinel format.")
    unit = _make_unit("Original [ev:abc].")

    out = await editor.edit(unit)

    assert out.edited_content == ""
    assert out.succeeded is False
    # Original citations weren't preserved (output is empty set != {[ev:abc]})
    assert out.citations_preserved is False


async def test_editor_tolerates_missing_end_sentinel():
    """If the LLM emits the start sentinel but forgets the end one,
    take everything after the start as the edited content. This is the
    most common partial-format failure mode."""
    editor, _llm = _editor(
        "=== EDITED START ===\nRewritten body [ev:abc] without an end marker."
    )
    unit = _make_unit("Original body [ev:abc].")

    out = await editor.edit(unit)

    assert "Rewritten body" in out.edited_content
    assert out.citations_preserved is True
    assert out.succeeded is True


async def test_editor_tolerates_preamble_before_start_sentinel():
    """If the LLM adds chatty preamble before the start sentinel
    (against instructions), still extract just the sentinel-bracketed part."""
    editor, _llm = _editor(
        "Sure, I'll rewrite this for you:\n\n"
        "=== EDITED START ===\nClean body [ev:abc].\n=== EDITED END ==="
    )
    unit = _make_unit("Original [ev:abc].")

    out = await editor.edit(unit)

    assert out.edited_content == "Clean body [ev:abc]."
    assert out.succeeded is True


async def test_editor_uses_configured_temperature():
    editor, llm = _editor(_edit_response("X [ev:abc]."))
    unit = _make_unit("Y [ev:abc].")

    await editor.edit(unit)

    # EditorConfig default temperature is 0.4
    assert llm.calls[0]["temperature"] == 0.4
    assert llm.calls[0]["system"] == EDITOR_SYSTEM_PROMPT


async def test_editor_does_not_count_underscore_citation_variant():
    """Canonical citation format is [ev:ID] (colon). The underscore variant
    [ev_ID] must NOT be recognized — a malformed marker in the writer's
    output should register as citation drift, not be silently accepted.
    """
    editor, _llm = _editor(_edit_response("Rewritten [ev_abc]."))  # underscore
    unit = _make_unit("Original [ev:abc].")  # colon

    out = await editor.edit(unit)

    # Input had 1 colon-form citation; output has 0 colon-form citations
    # (only the underscore variant, which the regex ignores). Drift detected.
    assert out.citations_preserved is False
