"""Citation index builder.

Scans a ContentUnit's content for [ev:ID] markers, assigns sequential
footnote indices by order of first appearance, replaces markers with
[^N] footnote syntax, and returns the ordered citation list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cce.models.evidence import Evidence

# Matches both citation formats produced by the writer:
#   [ev:ev_abc123]  — colon-separated (per writer prompt spec)
#   [ev_abc123]     — bare ID in brackets (common LLM output)
_EV_MARKER_RE = re.compile(r"\[ev:([^\]]+)\]|\[(ev_[^\]]+)\]")


@dataclass(frozen=True)
class CitationEntry:
    """A single citation in the footnote list."""

    id: str
    index: int  # 1-based
    url: str
    title: str | None
    author: str | None
    published_at: str | None  # ISO 8601 or None


@dataclass(frozen=True)
class CitationResult:
    """Output of the citation index builder."""

    content: str  # Body with [ev:ID] replaced by [^N]
    citations: tuple[CitationEntry, ...]  # Ordered by first appearance


def build_citation_index(
    content: str,
    evidence_by_id: dict[str, Evidence],
) -> CitationResult:
    """Scan content for [ev:ID] markers and build a footnote index.

    Args:
        content: Markdown body containing [ev:ID] inline markers.
        evidence_by_id: Lookup table of Evidence objects keyed by ID.

    Returns:
        CitationResult with transformed content and ordered citation list.

    Evidence IDs found in content but missing from evidence_by_id are
    replaced with [^?] and omitted from the citation list.
    """
    seen: dict[str, int] = {}  # evidence_id -> footnote index
    citations: list[CitationEntry] = []

    def _replace(match: re.Match[str]) -> str:
        # group(1) = colon format [ev:ID], group(2) = bare format [ev_ID]
        ev_id = match.group(1) or match.group(2)

        # The writer's prompt says "use [ev:EVIDENCE_ID]" while the evidence
        # block displays IDs as [ev_HASH] — the LLM frequently interprets
        # "EVIDENCE_ID" as just the HASH part (without the `ev_` prefix) and
        # emits [ev:HASH]. Try the literal lookup first, then re-try with
        # the `ev_` prefix added. Canonicalize ev_id so the seen[] cache
        # collapses both forms onto one footnote index.
        evidence = evidence_by_id.get(ev_id)
        if evidence is None and not ev_id.startswith("ev_"):
            prefixed = f"ev_{ev_id}"
            evidence = evidence_by_id.get(prefixed)
            if evidence is not None:
                ev_id = prefixed

        if ev_id in seen:
            return f"[^{seen[ev_id]}]"

        if evidence is None:
            return "[^?]"

        index = len(citations) + 1
        seen[ev_id] = index
        citations.append(
            CitationEntry(
                id=ev_id,
                index=index,
                url=evidence.url,
                title=evidence.title,
                author=evidence.author,
                published_at=(
                    evidence.published_at.isoformat() if evidence.published_at else None
                ),
            )
        )
        return f"[^{index}]"

    transformed = _EV_MARKER_RE.sub(_replace, content)
    return CitationResult(content=transformed, citations=tuple(citations))
