"""Shared LLM response parsing utilities."""

from __future__ import annotations

import json
import logging
import re

from cce.models.evidence import Evidence

logger = logging.getLogger(__name__)

# Citation-marker grammar shared by the quality gate and MDX emit (B4), so
# every marker the gate accepts is one emit can resolve. Matches both
# citation formats produced by the writer:
#   [ev:ev_abc123]  — colon-separated (per writer prompt spec)
#   [ev_abc123]     — bare ID in brackets (common LLM output)
EV_MARKER_RE = re.compile(r"\[ev:([^\]]+)\]|\[(ev_[^\]]+)\]")


def resolve_evidence_id(
    ev_id_raw: str, evidence_by_id: dict[str, Evidence]
) -> tuple[str, Evidence | None]:
    """Resolve a marker id to (canonical_ev_id, Evidence|None), retrying with the `ev_` prefix.

    The writer's prompt says "use [ev:EVIDENCE_ID]" while the evidence block displays IDs as
    [ev_HASH] — the LLM frequently interprets "EVIDENCE_ID" as just the HASH part (without the
    `ev_` prefix) and emits [ev:HASH]. Try the literal lookup first, then re-try with the `ev_`
    prefix added so downstream consumers see one canonical form. A leading ``ev:`` (the marker
    syntax copied into a citation list, as Haiku 4.5 does) is dropped when the literal ID
    doesn't resolve.
    """
    ev_id = ev_id_raw
    evidence = evidence_by_id.get(ev_id)
    if evidence is None and ev_id.startswith("ev:"):
        return resolve_evidence_id(ev_id[3:], evidence_by_id)
    if evidence is None and not ev_id.startswith("ev_"):
        prefixed = f"ev_{ev_id}"
        evidence = evidence_by_id.get(prefixed)
        if evidence is not None:
            ev_id = prefixed
    return ev_id, evidence


def extract_json(text: str) -> dict | None:
    """Extract a JSON object from LLM output, handling code fences and formatting."""
    # Normalize line endings and strip whitespace
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # strict=False throughout: models sometimes emit a raw newline inside a
    # long string value instead of the \n escape, which strict parsing
    # rejects as an invalid control character (3 of 6 Sonnet 5 writer
    # replies, 2026-09-23). The parsed string then holds the intended newline.

    # Try direct parse
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # Try code fence extraction (greedy — we expect one block)
    patterns = [
        r"```json\s*\n(.*)\n\s*```",
        r"```\s*\n(.*)\n\s*```",
    ]
    candidate = None
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            try:
                return json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue

    # Bracket-matching fallback: first '{' to last '}'
    if candidate is None:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last > first:
            candidate = text[first : last + 1]
            try:
                return json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                pass

    # Last resort: try to repair malformed JSON (e.g. unescaped quotes in string values)
    if candidate is not None:
        repaired = _repair_json(candidate)
        if repaired is not None:
            return repaired

    # Length only: the reply can hold confidential content, so none of it
    # is logged.
    logger.warning("extract_json failed: length=%d", len(text))
    return None


def _repair_json(text: str, max_repairs: int = 50) -> dict | None:
    """Attempt to repair JSON with unescaped quotes in string values.

    LLMs commonly produce JSON where quoted words like "lost" inside string
    values are not escaped. This function iteratively escapes the problematic
    quote at each error position until the JSON parses or repairs are exhausted.
    """
    for _ in range(max_repairs):
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as e:
            pos = e.pos
            if pos is None or pos >= len(text):
                return None

            # Find the unescaped quote to fix. The parser may have already
            # consumed it, so the error position can be past the quote.
            # Look for the nearest preceding unescaped double quote.
            fix_pos = None
            if text[pos] == '"' and pos > 0 and text[pos - 1] != "\\":
                fix_pos = pos
            else:
                # Scan backward from error position for unescaped quote
                for i in range(pos - 1, max(pos - 10, -1), -1):
                    if text[i] == '"' and (i == 0 or text[i - 1] != "\\"):
                        fix_pos = i
                        break

            if fix_pos is None:
                return None
            text = text[:fix_pos] + '\\"' + text[fix_pos + 1 :]
    logger.warning("_repair_json: exhausted %d repair attempts", max_repairs)
    return None
