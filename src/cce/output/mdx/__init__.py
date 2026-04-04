"""MDX emit — public API.

Converts a PublishPackage into a directory of .mdx files + companion JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cce.models.evidence import Evidence
from cce.models.package import PublishPackage
from cce.output.mdx.evidence import export_evidence
from cce.output.mdx.formatter import format_mdx_page
from cce.output.mdx.meta import merge_topic_meta


@dataclass(frozen=True)
class EmitResult:
    """Summary of what was written."""

    topic_slug: str
    target_dir: Path
    paths_written: list[str]
    files_written: int


def slugify(text: str) -> str:
    """Convert a topic name to a URL-safe slug.

    >>> slugify("Sleep Hygiene")
    'sleep-hygiene'
    >>> slugify("Anxiety & Stress")
    'anxiety-stress'
    """
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)  # strip non-word chars (except hyphen)
    s = re.sub(r"[\s_]+", "-", s)  # collapse whitespace/underscore to hyphen
    s = re.sub(r"-+", "-", s)  # collapse multiple hyphens
    return s.strip("-")


def emit_mdx(
    package: PublishPackage,
    target_dir: Path,
    topic_slug: str | None = None,
    topic_name: str | None = None,
) -> EmitResult:
    """Emit MDX files for a completed PublishPackage.

    Args:
        package: The completed curation package.
        target_dir: Root content directory (e.g. src/content/topics/).
        topic_slug: Explicit slug override. If None, derived from topic_name.
        topic_name: Topic name for slug derivation. Required if topic_slug is None.

    Returns:
        EmitResult summarizing what was written.

    Raises:
        ValueError: If neither topic_slug nor topic_name is provided.
    """
    if topic_slug is None:
        if topic_name is None:
            raise ValueError("Either topic_slug or topic_name must be provided")
        topic_slug = slugify(topic_name)

    # Build evidence lookup
    evidence_by_id: dict[str, Evidence] = {ev.id: ev for ev in package.evidence}

    topic_dir = target_dir / topic_slug
    topic_dir.mkdir(parents=True, exist_ok=True)

    curated_at = datetime.now(timezone.utc).isoformat()
    files_written = 0
    paths_written: list[str] = []

    # Write page.mdx per ContentUnit
    for unit in package.units:
        path_dir = topic_dir / unit.path
        path_dir.mkdir(parents=True, exist_ok=True)

        mdx_content = format_mdx_page(
            unit,
            evidence_by_id,
            package.job_id,
            topic_slug=topic_slug,
            curated_at=curated_at,
        )
        (path_dir / "page.mdx").write_text(mdx_content, encoding="utf-8")
        files_written += 1
        paths_written.append(unit.path)

    # Write _evidence.json
    evidence_json = export_evidence(package.units, evidence_by_id)
    (topic_dir / "_evidence.json").write_text(evidence_json, encoding="utf-8")
    files_written += 1

    # Merge meta.json
    merge_topic_meta(
        meta_path=topic_dir / "meta.json",
        scores=package.scores,
        lineage=package.lineage,
        job_id=package.job_id,
        curated_at=curated_at,
    )
    files_written += 1

    return EmitResult(
        topic_slug=topic_slug,
        target_dir=topic_dir,
        paths_written=paths_written,
        files_written=files_written,
    )
