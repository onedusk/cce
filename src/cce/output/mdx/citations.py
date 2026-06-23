"""Citation index builder.

Scans a ContentUnit's content for [ev:ID] markers, assigns one sequential
footnote index per unique source URL (by order of first appearance),
replaces markers with [^N] footnote syntax, and returns the ordered
citation list. Multiple evidence excerpts of the same URL collapse onto a
single footnote number (emit-time, per-article — ADR-001/002).
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

    id: str  # representative (first-seen) evidence id for this source
    index: int  # 1-based, one per UNIQUE source URL
    url: str
    title: str | None
    author: str | None
    published_at: str | None  # ISO 8601 or None


@dataclass(frozen=True)
class CitationResult:
    """Output of the citation index builder."""

    content: str  # Body with [ev:ID] replaced by [^N]
    citations: tuple[CitationEntry, ...]  # Ordered by first appearance


def _canonical_url(url: str) -> str:
    """Normalize a source URL so excerpts of the same page collapse to one footnote.

    Conservative: strips the fragment and trailing slash only. Query strings are left intact —
    they can be semantically significant for some sources, and over-collapsing would merge
    genuinely distinct pages.
    """
    return url.strip().split("#", 1)[0].rstrip("/")


def _resolve(
    ev_id_raw: str, evidence_by_id: dict[str, Evidence]
) -> tuple[str, Evidence | None]:
    """Resolve a marker id to (canonical_ev_id, Evidence|None), retrying with the `ev_` prefix.

    The writer's prompt says "use [ev:EVIDENCE_ID]" while the evidence block displays IDs as
    [ev_HASH] — the LLM frequently interprets "EVIDENCE_ID" as just the HASH part (without the
    `ev_` prefix) and emits [ev:HASH]. Try the literal lookup first, then re-try with the `ev_`
    prefix added so downstream consumers see one canonical form.
    """
    ev_id = ev_id_raw
    evidence = evidence_by_id.get(ev_id)
    if evidence is None and not ev_id.startswith("ev_"):
        prefixed = f"ev_{ev_id}"
        evidence = evidence_by_id.get(prefixed)
        if evidence is not None:
            ev_id = prefixed
    return ev_id, evidence


def build_citation_index(
    content: str,
    evidence_by_id: dict[str, Evidence],
) -> CitationResult:
    """Scan [ev:ID] markers; assign ONE footnote index per unique source URL (per article).

    Args:
        content: Markdown body containing [ev:ID] inline markers.
        evidence_by_id: Lookup table of Evidence objects keyed by ID.

    Returns:
        CitationResult with transformed content and ordered citation list.

    Evidence IDs found in content but missing from evidence_by_id are
    replaced with [^?] and omitted from the citation list. Multiple evidence
    IDs that resolve to the same canonical source URL share one footnote
    index; the citation entry's `id` is the first-seen (representative) id.
    """
    seen: dict[str, int] = {}  # canonical URL -> footnote index
    citations: list[CitationEntry] = []

    def _replace(match: re.Match[str]) -> str:
        # group(1) = colon format [ev:ID], group(2) = bare format [ev_ID]
        ev_id_raw = match.group(1) or match.group(2)
        ev_id, evidence = _resolve(ev_id_raw, evidence_by_id)
        if evidence is None:
            return "[^?]"
        key = _canonical_url(evidence.url)
        if key in seen:
            return f"[^{seen[key]}]"
        index = len(citations) + 1
        seen[key] = index
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
