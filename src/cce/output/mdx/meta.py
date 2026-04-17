"""Topic meta.json merge logic.

Reads an existing meta.json (if present), merges CCE-owned fields without
touching editorial fields, and writes the result back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cce.models.content import ContentScores
from cce.models.package import PackageLineage

# Fields that CCE owns and will overwrite on every emit.
CCE_FIELDS = frozenset(
    {
        "scores",
        "lastCuratedAt",
        "jobId",
        "policyId",
        "engineVersion",
        "evidenceGaps",
    }
)


def merge_topic_meta(
    meta_path: Path,
    scores: ContentScores,
    lineage: PackageLineage,
    job_id: str,
    curated_at: str,
    evidence_gaps: dict[str, list[str]] | None = None,
) -> None:
    """Read, merge, and write meta.json.

    If meta_path exists, reads the existing JSON, updates only CCE-owned
    fields, and writes back. If it doesn't exist, writes a new file with
    only CCE fields (no editorial defaults).

    Args:
        meta_path: Path to the meta.json file.
        scores: Aggregate ContentScores for the topic.
        lineage: PackageLineage for provenance fields.
        job_id: The curation job ID.
        curated_at: ISO 8601 timestamp of this emit.
        evidence_gaps: Optional per-path list of [INSUFFICIENT EVIDENCE: ...]
            descriptions stripped from the rendered MDX bodies. Surfaced so
            operators can audit gaps without readers seeing internal
            scaffolding in published copy. ``None`` clears the field entirely.
    """
    existing: dict = {}
    if meta_path.exists():
        existing = json.loads(meta_path.read_text(encoding="utf-8"))

    cce_data: dict = {
        "scores": {
            "confidence": round(scores.confidence, 3),
            "coverage": round(scores.coverage, 3),
            "sourceDiversity": round(scores.source_diversity, 3),
        },
        "lastCuratedAt": curated_at,
        "jobId": job_id,
        "policyId": lineage.policy_id,
        "engineVersion": lineage.engine_version,
    }
    # evidenceGaps is set when present; cleared (key removed) when None
    # to avoid stale gap reports lingering across re-runs.
    if evidence_gaps:
        cce_data["evidenceGaps"] = evidence_gaps
    elif "evidenceGaps" in existing:
        del existing["evidenceGaps"]

    existing.update(cce_data)

    # Atomic write: write to temp file then rename to avoid corrupting
    # editorial content if the process crashes mid-write.
    tmp_path = meta_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, meta_path)
