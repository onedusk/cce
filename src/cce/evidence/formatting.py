"""Shared evidence formatting for LLM prompts.

Used by both the Writer and Verifier to format evidence objects
into reference blocks for their respective prompts.

Prompt-injection stance (B13, SECURITY.md): crawled and model-written text
reaches a prompt only inside an element the prompt builder writes itself
(``<evidence id="...">`` here, ``<draft>`` via ``quote_untrusted``), after
``defang`` has made sure the text cannot open or close such an element or
forge one of the prompts' ``=== ... ===`` fences (which the Anthropic
provider also splits the prompt cache on). Deterministic, no nonce, so the
cached prefix stays byte-stable.
"""

from __future__ import annotations

import html
import re

from cce.models.evidence import Evidence

# Element names the prompts reserve for untrusted text.
UNTRUSTED_TAGS = ("evidence", "draft")
# "<" of anything a model could read as one of those tags opening or closing,
# loosely spelled: </EVIDENCE>, < /evidence >, <Draft id=x>.
_TAG_OPEN_RE = re.compile(
    rf"<(?=\s*/?\s*(?:{'|'.join(UNTRUSTED_TAGS)})\b)", re.IGNORECASE
)
# A prompt fence: a run of 3+ "=" around upper-case words on one line
# (=== EVIDENCE END ===). Upper case, and no operators inside, so code such
# as ``a === b && c === d`` is left alone.
_FENCE_RE = re.compile(r"={3,}([ \t]*[A-Z][A-Za-z0-9 ()/,.'-]*?)={3,}")


def defang(text: str) -> str:
    """Neutralise reserved tags and fences in untrusted text. Idempotent,
    and the identity on text that contains neither."""
    text = _FENCE_RE.sub(lambda m: f"=={m.group(1)}==", text)
    return _TAG_OPEN_RE.sub("&lt;", text)


def quote_untrusted(tag: str, text: str) -> str:
    """Wrap untrusted text in one of the reserved elements, defanged."""
    if tag not in UNTRUSTED_TAGS:
        raise ValueError(f"not a reserved element: {tag!r}")
    return f"<{tag}>\n{defang(text)}\n</{tag}>"


def _one_line(text: str) -> str:
    """Metadata (URL, title, author) on one line, defanged."""
    return defang(" ".join(text.split()))


def _open_evidence(ev: Evidence) -> str:
    return f'<evidence id="{html.escape(ev.id, quote=True)}">'


def format_evidence_for_prompt(
    evidence: list[Evidence],
    *,
    style: str = "writer",
) -> str:
    """Format evidence objects as a reference block for LLM prompts.

    Args:
        evidence: List of Evidence objects to format.
        style: "writer" for synthesis prompts (detailed metadata),
            "verifier" for verification prompts (compact with quality tags).

    Returns:
        Formatted string with one evidence block per object.
    """
    if style == "verifier":
        return _format_verifier(evidence)
    return _format_writer(evidence)


def _format_writer(evidence: list[Evidence]) -> str:
    """Detailed format with metadata for the writer prompt."""
    lines: list[str] = []
    for ev in evidence:
        meta_parts = [f"URL: {_one_line(ev.url)}"]
        if ev.title:
            meta_parts.append(f"Title: {_one_line(ev.title)}")
        if ev.author:
            meta_parts.append(f"Author: {_one_line(ev.author)}")
        if ev.published_at:
            meta_parts.append(f"Published: {ev.published_at.strftime('%Y-%m-%d')}")
        if ev.source_quality and ev.source_quality.domain_reputation:
            meta_parts.append(f"Reputation: {ev.source_quality.domain_reputation}")
        if ev.source_quality and ev.source_quality.is_peer_reviewed:
            meta_parts.append("Type: peer-reviewed")
        if ev.source_quality and ev.source_quality.is_primary_source:
            meta_parts.append("Type: primary-source")

        lines.append(_open_evidence(ev))
        lines.append(f"--- EVIDENCE [{ev.id}] ---")
        lines.append(" | ".join(meta_parts))
        lines.append(defang(ev.excerpt))
        lines.append("</evidence>")
        lines.append("")

    return "\n".join(lines)


def _format_verifier(evidence: list[Evidence]) -> str:
    """Compact format with quality tags for the verifier prompt."""
    lines: list[str] = []
    for ev in evidence:
        tags: list[str] = []
        if ev.source_quality:
            if ev.source_quality.is_peer_reviewed:
                tags.append("peer-reviewed")
            if ev.source_quality.is_primary_source:
                tags.append("primary-source")
            if ev.source_quality.conflict_of_interest:
                tags.append("potential-COI")
        header = f"[{ev.id}] (URL: {_one_line(ev.url)})"
        if tags:
            header += " [" + "] [".join(tags) + "]"
        lines.append(_open_evidence(ev))
        lines.append(header)
        lines.append(defang(ev.excerpt))
        lines.append("</evidence>")
        lines.append("")
    return "\n".join(lines)
