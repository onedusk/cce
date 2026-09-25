"""Byte-identity guard for MDX emit (bubble-readiness Phase 2).

Emits a fixed package through both formats (generic and the client site
format) with the clock pinned, and compares every file byte for byte with
the committed goldens in ``golden/``. Captured before B12 (citation keying)
and B13 (sanitisation) touched emit, so default-mode output of those items
is proven unchanged. The fixture is clean prose — no ``{ } <``, ESM lines or
inline-special characters — so an escaper that is the identity on clean
text keeps it byte-identical.

When a change *intends* to alter emit output, regenerate with
``uv run python -m tests.test_mdx_emit.test_golden_emit`` and review the
diff. (The directory is not named after the client format: ``.gitignore``
ignores ``*thnk*`` paths.)
"""

from __future__ import annotations

import filecmp
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cce.models.content import (
    Citation,
    ClaimMapping,
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence, SourceQuality
from cce.models.package import PackageLineage, PublishPackage
from cce.output.mdx import emit_mdx
from cce.output.mdx.thnklabs import emit_thnklabs

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden"
FORMATS = {"generic": emit_mdx, "client": emit_thnklabs}
FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FIXED_NOW


def _evidence(ev_id: str, url: str, excerpt: str, locator: str, **kw) -> Evidence:
    import hashlib

    return Evidence(
        id=ev_id,
        url=url,
        title=kw.get("title", "Sleep and the Body"),
        author=kw.get("author", "A. Author"),
        published_at=datetime(2024, 1, 15, tzinfo=UTC),
        retrieved_at=datetime(2024, 3, 1, tzinfo=UTC),
        excerpt=excerpt,
        excerpt_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
        locator=locator,
        source_quality=kw.get("source_quality"),
        tags=kw.get("tags", []),
    )


def golden_package() -> PublishPackage:
    """Deterministic package: two excerpts of one URL (different locators),
    a second URL, a gap marker, and an explore path with a resources list."""
    ev = [
        _evidence(
            "ev_aaaa00000001",
            "https://sleep.example.org/guide",
            "Adults need seven or more hours of sleep per night.",
            "chunk:0",
            source_quality=SourceQuality(is_primary_source=True),
            tags=["sleep"],
        ),
        _evidence(
            "ev_aaaa00000002",
            "https://sleep.example.org/guide",
            "A regular wake time anchors the body clock.",
            "chunk:3",
        ),
        _evidence(
            "ev_bbbb00000001",
            "https://journal.example.com/study",
            "Light exposure in the evening delays melatonin release.",
            "chunk:1",
            title="Evening Light and Melatonin",
            author="B. Researcher",
            source_quality=SourceQuality(is_peer_reviewed=True),
        ),
    ]
    lineage = ContentLineage(
        policy_id="peer-reviewed", run_id="run_golden", engine_version="0.1.0-golden"
    )
    learn = ContentUnit(
        id="cu_learn",
        path="learn",
        content=(
            "## How Much Sleep Adults Need\n\n"
            "Most adults need seven or more hours a night [ev:ev_aaaa00000001]. "
            "A steady wake time matters as much as the total [ev:ev_aaaa00000002].\n\n"
            "## Light in the Evening\n\n"
            "Bright evening light pushes melatonin later [ev:ev_bbbb00000001], "
            "which is why screens before bed make it harder to fall asleep "
            "[ev_bbbb00000001].\n\n"
            "[INSUFFICIENT EVIDENCE: no data on napping]"
        ),
        citations=[Citation(evidence_id=e.id, url=e.url) for e in ev],
        evidence_map=[
            ClaimMapping(claim="Adults need 7+ hours", evidence_ids=["ev_aaaa00000001"])
        ],
        scores=ContentScores(confidence=0.9, coverage=0.85, source_diversity=0.5),
        lineage=lineage,
        tags=["sleep"],
    )
    explore = ContentUnit(
        id="cu_explore",
        path="explore",
        content=(
            "## Where Sleep Fits\n\n"
            "Sleep ties into the body clock [ev:ev_aaaa00000002].\n\n"
            "## Curated Resources\n\n"
            "- A resource the writer listed itself\n"
        ),
        citations=[Citation(evidence_id=ev[1].id, url=ev[1].url)],
        evidence_map=[],
        scores=ContentScores(confidence=0.95, coverage=0.9, source_diversity=0.25),
        lineage=lineage,
    )
    return PublishPackage(
        job_id="job_golden",
        units=[learn, explore],
        evidence=ev,
        scores=ContentScores(confidence=0.925, coverage=0.875, source_diversity=0.375),
        lineage=PackageLineage(
            policy_id="peer-reviewed",
            run_id="run_golden",
            engine_version="0.1.0-golden",
        ),
    )


def _emit(fmt: str, target: Path, monkeypatch=None) -> None:
    import cce.output.mdx as generic_module
    import cce.output.mdx.thnklabs as client_module

    if monkeypatch is not None:
        monkeypatch.setattr(generic_module, "datetime", _FixedDatetime)
        monkeypatch.setattr(client_module, "datetime", _FixedDatetime)
    else:  # regeneration entry point
        generic_module.datetime = _FixedDatetime  # type: ignore[misc]
        client_module.datetime = _FixedDatetime  # type: ignore[misc]
    FORMATS[fmt](golden_package(), target, topic_slug="sleep-basics")


def _files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_emit_output_is_byte_identical_to_golden(fmt, tmp_path, monkeypatch):
    _emit(fmt, tmp_path, monkeypatch)
    expected_root = GOLDEN / fmt

    assert _files(tmp_path) == _files(expected_root)
    for rel in _files(expected_root):
        assert filecmp.cmp(tmp_path / rel, expected_root / rel, shallow=False), (
            f"{fmt}/{rel} differs from the golden — if intended, regenerate "
            "(see module docstring) and review the diff"
        )


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    import shutil

    for fmt in FORMATS:
        shutil.rmtree(GOLDEN / fmt, ignore_errors=True)
        (GOLDEN / fmt).mkdir(parents=True)
        _emit(fmt, GOLDEN / fmt)
        print(f"regenerated {GOLDEN / fmt}")
