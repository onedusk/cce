"""Shared evidence formatting for LLM prompts.

Used by both the Writer and Verifier to format evidence objects
into reference blocks for their respective prompts.
"""

from __future__ import annotations

from cce.models.evidence import Evidence


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
        meta_parts = [f"URL: {ev.url}"]
        if ev.title:
            meta_parts.append(f"Title: {ev.title}")
        if ev.author:
            meta_parts.append(f"Author: {ev.author}")
        if ev.published_at:
            meta_parts.append(f"Published: {ev.published_at.strftime('%Y-%m-%d')}")
        if ev.source_quality and ev.source_quality.domain_reputation:
            meta_parts.append(f"Reputation: {ev.source_quality.domain_reputation}")
        if ev.source_quality and ev.source_quality.is_peer_reviewed:
            meta_parts.append("Type: peer-reviewed")
        if ev.source_quality and ev.source_quality.is_primary_source:
            meta_parts.append("Type: primary-source")

        lines.append(f"--- EVIDENCE [{ev.id}] ---")
        lines.append(" | ".join(meta_parts))
        lines.append(ev.excerpt)
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
        header = f"[{ev.id}] (URL: {ev.url})"
        if tags:
            header += " [" + "] [".join(tags) + "]"
        lines.append(header)
        lines.append(ev.excerpt)
        lines.append("")
    return "\n".join(lines)
