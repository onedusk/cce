"""MDX page formatter.

Builds the content of a page.mdx file: an `export const metadata = {...}`
block followed by the footnoted markdown body.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime  # used when curated_at is None

from cce.models.content import ContentUnit
from cce.models.evidence import Evidence
from cce.output.mdx.citations import CitationEntry, build_citation_index


def format_mdx_page(
    unit: ContentUnit,
    evidence_by_id: dict[str, Evidence],
    job_id: str,
    *,
    topic_slug: str = "",
    curated_at: str | None = None,
) -> str:
    """Produce a complete page.mdx string for a single ContentUnit.

    Args:
        unit: The ContentUnit to format.
        evidence_by_id: All evidence objects keyed by ID (for citation lookup).
        job_id: The curation job ID (included in metadata).
        topic_slug: Topic slug for the metadata.topic field.
        curated_at: ISO 8601 timestamp. If None, uses current UTC time.

    Returns:
        A string containing the full MDX file content.
    """
    result = build_citation_index(unit.content, evidence_by_id)

    if curated_at is None:
        curated_at = datetime.now(UTC).isoformat()

    metadata = {
        "title": _derive_title(result.content),
        "topic": topic_slug,
        "path": unit.path,
        "dimensions": [],
        "tags": unit.tags,
        "scores": {
            "confidence": round(unit.scores.confidence, 3),
            "coverage": round(unit.scores.coverage, 3),
            "sourceDiversity": round(unit.scores.source_diversity, 3),
        },
        "curationJob": job_id,
        "policyId": unit.lineage.policy_id,
        "taxonomyId": unit.lineage.taxonomy_id or None,
        "pathConfigId": unit.lineage.path_config_id or None,
        "engineVersion": unit.lineage.engine_version,
        "curatedAt": curated_at,
        "status": "draft",
        "citations": [_citation_to_dict(c) for c in result.citations],
    }

    metadata_json = json.dumps(metadata, indent=2, ensure_ascii=False)
    return f"export const metadata = {metadata_json}\n\n{result.content}\n"


def _derive_title(content: str) -> str:
    """Extract the first heading (any level) as the page title."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return ""


def _citation_to_dict(entry: CitationEntry) -> dict:
    """Serialize a CitationEntry for the metadata export."""
    d: dict = {
        "id": entry.id,
        "index": entry.index,
        "url": entry.url,
    }
    if entry.title is not None:
        d["title"] = " ".join(entry.title.split())
    if entry.author is not None:
        d["author"] = " ".join(entry.author.split())
    if entry.published_at is not None:
        d["publishedAt"] = entry.published_at
    return d
