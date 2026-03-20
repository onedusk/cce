"""Shared LLM response parsing utilities."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict | None:
    """Extract a JSON object from LLM output, handling code fences and formatting."""
    # Normalize line endings and strip whitespace
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Try direct parse
    try:
        return json.loads(text)
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
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # Bracket-matching fallback: first '{' to last '}'
    if candidate is None:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last > first:
            candidate = text[first : last + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Last resort: try to repair malformed JSON (e.g. unescaped quotes in string values)
    if candidate is not None:
        repaired = _repair_json(candidate)
        if repaired is not None:
            return repaired

    logger.warning(
        "extract_json failed: length=%d, starts=%r",
        len(text),
        text[:100],
    )
    return None


def _repair_json(text: str, max_repairs: int = 50) -> dict | None:
    """Attempt to repair JSON with unescaped quotes in string values.

    LLMs commonly produce JSON where quoted words like "lost" inside string
    values are not escaped. This function iteratively escapes the problematic
    quote at each error position until the JSON parses or repairs are exhausted.
    """
    for _ in range(max_repairs):
        try:
            return json.loads(text)
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
