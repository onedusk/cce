"""thnkLabs-format MDX emit (operator-specific variant of the generic emitter).

The generic emitter (`output/mdx/formatter.format_mdx_page`) writes an
engine-shaped metadata block (`topic`, `scores`, `curationJob`, …). The thnkLabs
site expects the `ArticleMetadata` shape (`src/lib/content-types.ts`):
`title, topicSlug, path, excerpt, tags, readTime, dimensions, status` plus an
optional `citations`, closed as ``export const metadata = { ... };`` (JS, note
the trailing semicolon).

This module reshapes the SAME `PublishPackage` into that format. Everything
except the page.mdx metadata is reused verbatim:
- body + footnotes: `build_citation_index` (URL-keyed per M02),
- `_evidence.json`: `export_evidence` (already byte-identical to deployed),
- `meta.json`: `merge_topic_meta` (preserves editorial fields, updates CCE ones).
Only `format_thnklabs_page` differs from `format_mdx_page`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from cce.models.content import ContentUnit
from cce.models.evidence import Evidence
from cce.models.package import PublishPackage
from cce.output.mdx import EmitResult, _strip_evidence_gaps, slugify
from cce.output.mdx.citations import build_citation_index
from cce.output.mdx.evidence import export_evidence
from cce.output.mdx.formatter import _citation_to_dict, _derive_title
from cce.output.mdx.meta import merge_topic_meta

_WORDS_PER_MINUTE = 200  # observed across deployed thnkLabs articles (~200-210 wpm)
_FOOTNOTE_RE = re.compile(r"\[\^(?:[0-9]+|\?)\]")  # rendered markers, stripped from excerpt


def _read_time(body: str) -> int:
    """Estimated reading time in minutes: round(words / 200), floor 1."""
    return max(1, round(len(body.split()) / _WORDS_PER_MINUTE))


def _derive_excerpt(body: str, limit: int = 200) -> str:
    """First substantive paragraph — headings skipped, footnote markers stripped,
    whitespace collapsed — truncated on a word boundary near ``limit`` with an
    ellipsis (matches the deployed excerpt style)."""
    for block in body.split("\n\n"):
        text = block.strip()
        if not text or text.startswith("#"):
            continue
        text = re.sub(r"\s+", " ", _FOOTNOTE_RE.sub("", text)).strip()
        if not text:
            continue
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "…"
    return ""


def format_thnklabs_page(
    unit: ContentUnit,
    evidence_by_id: dict[str, Evidence],
    *,
    topic_slug: str,
    status: str = "draft",
    curated_at: str | None = None,
) -> str:
    """Render one ContentUnit as a thnkLabs `page.mdx` (ArticleMetadata + body)."""
    result = build_citation_index(unit.content, evidence_by_id)
    body = result.content

    metadata: dict = {
        "title": _derive_title(body),
        "topicSlug": topic_slug,
        "path": unit.path,
        "excerpt": _derive_excerpt(body),
        "tags": unit.tags,
        "readTime": _read_time(body),
        "dimensions": [],  # topic-level dimensions live in meta.json, not the page
        "status": status,
    }
    if result.citations:
        metadata["citations"] = [_citation_to_dict(c) for c in result.citations]
    # Optional provenance fields (ArticleMetadata.scores? / curatedAt?).
    metadata["scores"] = {
        "confidence": round(unit.scores.confidence, 3),
        "coverage": round(unit.scores.coverage, 3),
        "sourceDiversity": round(unit.scores.source_diversity, 3),
    }
    if curated_at is not None:
        metadata["curatedAt"] = curated_at

    metadata_json = json.dumps(metadata, indent=2, ensure_ascii=False)
    return f"export const metadata = {metadata_json};\n\n{body}\n"


def emit_thnklabs(
    package: PublishPackage,
    target_dir: Path,
    topic_slug: str | None = None,
    topic_name: str | None = None,
    *,
    status: str = "draft",
) -> EmitResult:
    """Emit a completed package as thnkLabs MDX (page.mdx per path + _evidence.json + meta.json).

    Mirrors `emit_mdx` exactly but renders page.mdx via `format_thnklabs_page`.
    `status` is the ArticleMetadata status for fresh emits (default "draft" —
    do not publish unreviewed regen to the live site without intent).
    """
    if topic_slug is None:
        if topic_name is None:
            raise ValueError("Either topic_slug or topic_name must be provided")
        topic_slug = slugify(topic_name)

    evidence_by_id: dict[str, Evidence] = {ev.id: ev for ev in package.evidence}
    topic_dir = target_dir / topic_slug
    topic_dir.mkdir(parents=True, exist_ok=True)

    curated_at = datetime.now(UTC).isoformat()
    files_written = 0
    paths_written: list[str] = []
    evidence_gaps_by_path: dict[str, list[str]] = {}

    for unit in package.units:
        path_dir = topic_dir / unit.path
        path_dir.mkdir(parents=True, exist_ok=True)

        cleaned_content, gaps = _strip_evidence_gaps(unit.content)
        if gaps:
            evidence_gaps_by_path[unit.path] = gaps
        if cleaned_content != unit.content:
            unit = unit.model_copy(update={"content": cleaned_content})

        mdx_content = format_thnklabs_page(
            unit,
            evidence_by_id,
            topic_slug=topic_slug,
            status=status,
            curated_at=curated_at,
        )
        (path_dir / "page.mdx").write_text(mdx_content, encoding="utf-8")
        files_written += 1
        paths_written.append(unit.path)

    (topic_dir / "_evidence.json").write_text(
        export_evidence(package.units, evidence_by_id), encoding="utf-8"
    )
    files_written += 1

    merge_topic_meta(
        meta_path=topic_dir / "meta.json",
        scores=package.scores,
        lineage=package.lineage,
        job_id=package.job_id,
        curated_at=curated_at,
        evidence_gaps=evidence_gaps_by_path or None,
    )
    files_written += 1

    return EmitResult(
        topic_slug=topic_slug,
        target_dir=topic_dir,
        paths_written=paths_written,
        files_written=files_written,
    )
