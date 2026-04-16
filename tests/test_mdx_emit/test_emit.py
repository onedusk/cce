"""Tests for cce.output.mdx — emit_mdx orchestrator and slugify."""

from __future__ import annotations

import json

import pytest

from cce.models.content import Citation, ContentScores
from cce.output.mdx import EmitResult, emit_mdx, slugify
from tests.conftest import make_content_unit, make_evidence, make_publish_package


def _package_with_paths(*paths: str):
    """Build a PublishPackage with units for the given paths."""
    ev_a = make_evidence(id="ev_a", url="https://a.com")
    ev_b = make_evidence(id="ev_b", url="https://b.com")
    units = [
        make_content_unit(
            path=p,
            content=f"## {p.title()} Content\n\nClaim one [ev:ev_a] and two [ev:ev_b].",
            citations=[
                Citation(evidence_id="ev_a", url="https://a.com"),
                Citation(evidence_id="ev_b", url="https://b.com"),
            ],
        )
        for p in paths
    ]
    return make_publish_package(
        units=units,
        evidence=[ev_a, ev_b],
    )


class TestSlugify:
    def test_basic(self):
        assert slugify("Sleep Hygiene") == "sleep-hygiene"

    def test_special_chars(self):
        assert slugify("Anxiety & Stress") == "anxiety-stress"

    def test_already_slug(self):
        assert slugify("anxiety") == "anxiety"

    def test_multiple_spaces(self):
        assert slugify("  lots   of   spaces  ") == "lots-of-spaces"


class TestEmitMdx:
    def test_full_emit(self, tmp_path):
        pkg = _package_with_paths("learn", "explore")
        result = emit_mdx(pkg, tmp_path, topic_slug="anxiety")

        assert (tmp_path / "anxiety" / "learn" / "page.mdx").exists()
        assert (tmp_path / "anxiety" / "explore" / "page.mdx").exists()
        assert (tmp_path / "anxiety" / "_evidence.json").exists()
        assert (tmp_path / "anxiety" / "meta.json").exists()

    def test_page_content_validity(self, tmp_path):
        pkg = _package_with_paths("learn")
        emit_mdx(pkg, tmp_path, topic_slug="test")

        mdx = (tmp_path / "test" / "learn" / "page.mdx").read_text()
        assert mdx.startswith("export const metadata = ")
        assert "[ev:" not in mdx
        assert "[^1]" in mdx

    def test_evidence_dedup(self, tmp_path):
        pkg = _package_with_paths("learn", "explore")
        emit_mdx(pkg, tmp_path, topic_slug="test")

        evidence = json.loads(
            (tmp_path / "test" / "_evidence.json").read_text()
        )
        ids = [e["id"] for e in evidence]
        assert ids == list(dict.fromkeys(ids))  # no duplicates

    def test_meta_merge_preservation(self, tmp_path):
        topic_dir = tmp_path / "test"
        topic_dir.mkdir()
        meta_path = topic_dir / "meta.json"
        meta_path.write_text(json.dumps({
            "slug": "test",
            "title": "Test Topic",
            "orientationCopy": "Editorial text",
        }))

        pkg = _package_with_paths("learn")
        emit_mdx(pkg, tmp_path, topic_slug="test")

        meta = json.loads(meta_path.read_text())
        assert meta["slug"] == "test"
        assert meta["title"] == "Test Topic"
        assert meta["orientationCopy"] == "Editorial text"
        assert "scores" in meta
        assert "jobId" in meta

    def test_reemit_updates_meta(self, tmp_path):
        pkg1 = make_publish_package(
            units=[make_content_unit(path="learn")],
            scores=ContentScores(confidence=0.7, coverage=0.6, source_diversity=0.5),
        )
        pkg2 = make_publish_package(
            units=[make_content_unit(path="learn")],
            scores=ContentScores(confidence=0.95, coverage=0.92, source_diversity=0.88),
        )

        emit_mdx(pkg1, tmp_path, topic_slug="test")
        emit_mdx(pkg2, tmp_path, topic_slug="test")

        meta = json.loads((tmp_path / "test" / "meta.json").read_text())
        assert meta["scores"]["confidence"] == 0.95

    def test_partial_emit_preserves_other_paths(self, tmp_path):
        # Pre-create an explore page
        explore_dir = tmp_path / "test" / "explore"
        explore_dir.mkdir(parents=True)
        (explore_dir / "page.mdx").write_text("existing explore content")

        # Emit only learn
        pkg = _package_with_paths("learn")
        emit_mdx(pkg, tmp_path, topic_slug="test")

        assert (tmp_path / "test" / "learn" / "page.mdx").exists()
        assert (tmp_path / "test" / "explore" / "page.mdx").read_text() == "existing explore content"

    def test_slugify_integration(self, tmp_path):
        pkg = _package_with_paths("learn")
        result = emit_mdx(pkg, tmp_path, topic_name="Sleep Hygiene")

        assert result.topic_slug == "sleep-hygiene"
        assert (tmp_path / "sleep-hygiene" / "learn" / "page.mdx").exists()

    def test_emit_result_accuracy(self, tmp_path):
        pkg = _package_with_paths("learn", "explore")
        result = emit_mdx(pkg, tmp_path, topic_slug="t")

        assert isinstance(result, EmitResult)
        assert result.topic_slug == "t"
        assert result.target_dir == tmp_path / "t"
        assert set(result.paths_written) == {"learn", "explore"}
        assert result.files_written == 4  # 2 pages + evidence + meta

    def test_missing_slug_and_name_raises(self, tmp_path):
        pkg = _package_with_paths("learn")
        with pytest.raises(ValueError, match="Either topic_slug or topic_name"):
            emit_mdx(pkg, tmp_path)
