"""Evidence JSON exporter.

Serializes and deduplicates evidence objects for a topic's _evidence.json.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from cce.models.content import ContentUnit
from cce.models.evidence import Evidence


def export_evidence(
    units: list[ContentUnit],
    evidence_by_id: dict[str, Evidence],
    path_order: tuple[str, ...] = ("learn", "explore", "apply"),
) -> str:
    """Produce the _evidence.json content string.

    Evidence is deduplicated and ordered by first citation across paths,
    following path_order (learn -> explore -> apply by default).

    Args:
        units: ContentUnits from the package.
        evidence_by_id: All evidence objects keyed by ID.
        path_order: Order in which paths are scanned for first citation.

    Returns:
        A JSON string (pretty-printed) of the evidence array.
    """
    seen: OrderedDict[str, Evidence] = OrderedDict()

    sorted_units = sorted(
        units,
        key=lambda u: (
            path_order.index(u.path) if u.path in path_order else len(path_order)
        ),
    )

    for unit in sorted_units:
        for citation in unit.citations:
            if citation.evidence_id not in seen:
                ev = evidence_by_id.get(citation.evidence_id)
                if ev is not None:
                    seen[citation.evidence_id] = ev

    evidence_list = [_serialize_evidence(ev) for ev in seen.values()]
    return json.dumps(evidence_list, indent=2, ensure_ascii=False)


def _serialize_evidence(ev: Evidence) -> dict:
    """Serialize a single Evidence object for _evidence.json."""
    d: dict = {
        "id": ev.id,
        "url": ev.url,
    }
    if ev.title is not None:
        d["title"] = ev.title
    if ev.author is not None:
        d["author"] = ev.author
    if ev.published_at is not None:
        d["publishedAt"] = ev.published_at.isoformat()
    d["retrievedAt"] = ev.retrieved_at.isoformat()
    d["excerpt"] = ev.excerpt
    if ev.source_quality is not None:
        d["sourceQuality"] = {
            "peer_reviewed": ev.source_quality.is_peer_reviewed,
            "primary_source": ev.source_quality.is_primary_source,
            "domain_reputation": ev.source_quality.domain_reputation,
            "conflict_of_interest": ev.source_quality.conflict_of_interest,
        }
    if ev.tags:
        d["tags"] = ev.tags
    if ev.dimension_signals:
        d["dimensionSignals"] = ev.dimension_signals
    return d
