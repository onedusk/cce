"""Tests for cce.output.mdx.formatter — MDX page formatter."""

from __future__ import annotations

import json

from cce.models.content import Citation
from cce.output.mdx.formatter import _derive_title, format_mdx_page
from tests.conftest import make_content_unit, make_evidence


def _parse_metadata(mdx: str) -> dict:
    """Extract and parse the metadata JSON from an MDX string."""
    prefix = "export const metadata = "
    assert mdx.startswith(prefix)
    # Find the end of the JSON block (blank line separates metadata from body)
    json_end = mdx.index("\n\n", len(prefix))
    return json.loads(mdx[len(prefix) : json_end])


def _extract_body(mdx: str) -> str:
    """Extract the body content after the metadata block."""
    parts = mdx.split("\n\n", 1)
    return parts[1] if len(parts) > 1 else ""


class TestFormatMdxPage:
    def test_basic_page(self):
        ev = make_evidence(id="ev_001", url="https://example.com/1")
        unit = make_content_unit(
            path="learn",
            content="## Understanding Anxiety\n\nA claim [ev:ev_001].",
            citations=[Citation(evidence_id="ev_001", url="https://example.com/1")],
        )
        result = format_mdx_page(unit, {"ev_001": ev}, "job_abc")

        assert result.startswith("export const metadata = ")
        meta = _parse_metadata(result)
        assert meta["path"] == "learn"
        assert meta["status"] == "draft"
        assert "[^1]" in _extract_body(result)

    def test_metadata_shape_and_camel_case(self):
        ev = make_evidence(id="ev_x")
        unit = make_content_unit(
            content="Claim [ev:ev_x].",
            citations=[Citation(evidence_id="ev_x", url=ev.url)],
            tags=["emotional"],
        )
        meta = _parse_metadata(format_mdx_page(unit, {"ev_x": ev}, "job_1"))

        assert "sourceDiversity" in meta["scores"]
        assert "source_diversity" not in meta["scores"]
        assert isinstance(meta["tags"], list)
        assert meta["tags"] == ["emotional"]
        assert meta["curationJob"] == "job_1"
        assert meta["status"] == "draft"
        assert "curatedAt" in meta
        assert "policyId" in meta
        assert "engineVersion" in meta

    def test_title_extraction_h2(self):
        unit = make_content_unit(content="## My Title\n\nBody text.")
        meta = _parse_metadata(format_mdx_page(unit, {}, "j"))
        assert meta["title"] == "My Title"

    def test_title_extraction_no_heading(self):
        unit = make_content_unit(content="No heading here, just text.")
        meta = _parse_metadata(format_mdx_page(unit, {}, "j"))
        assert meta["title"] == ""

    def test_citations_array_matches_evidence(self):
        ev_a = make_evidence(id="ev_a", url="https://a.com")
        ev_b = make_evidence(id="ev_b", url="https://b.com")
        unit = make_content_unit(
            content="First [ev:ev_a] second [ev:ev_b].",
            citations=[
                Citation(evidence_id="ev_a", url="https://a.com"),
                Citation(evidence_id="ev_b", url="https://b.com"),
            ],
        )
        meta = _parse_metadata(
            format_mdx_page(unit, {"ev_a": ev_a, "ev_b": ev_b}, "j")
        )

        assert len(meta["citations"]) == 2
        assert meta["citations"][0]["id"] == "ev_a"
        assert meta["citations"][0]["index"] == 1
        assert meta["citations"][1]["id"] == "ev_b"
        assert meta["citations"][1]["index"] == 2

    def test_no_residual_ev_markers(self):
        ev = make_evidence(id="ev_z")
        unit = make_content_unit(
            content="Claim [ev:ev_z] and more [ev:ev_z].",
            citations=[Citation(evidence_id="ev_z", url=ev.url)],
        )
        result = format_mdx_page(unit, {"ev_z": ev}, "j")
        body = _extract_body(result)

        assert "[ev:" not in body

    def test_optional_citation_fields_omitted(self):
        ev = make_evidence(id="ev_bare", title=None, author=None, published_at=None)
        unit = make_content_unit(
            content="Claim [ev:ev_bare].",
            citations=[Citation(evidence_id="ev_bare", url=ev.url)],
        )
        meta = _parse_metadata(format_mdx_page(unit, {"ev_bare": ev}, "j"))

        cit = meta["citations"][0]
        assert "title" not in cit
        assert "author" not in cit
        assert "publishedAt" not in cit


    def test_topic_slug_and_curated_at_passthrough(self):
        unit = make_content_unit(content="Body text.")
        meta = _parse_metadata(format_mdx_page(
            unit, {}, "j",
            topic_slug="anxiety",
            curated_at="2026-04-01T12:00:00+00:00",
        ))

        assert meta["topic"] == "anxiety"
        assert meta["curatedAt"] == "2026-04-01T12:00:00+00:00"


class TestDeriveTitle:
    def test_h2(self):
        assert _derive_title("## My Title\nBody") == "My Title"

    def test_h1(self):
        assert _derive_title("# Top Heading\nBody") == "Top Heading"

    def test_first_heading_wins(self):
        assert _derive_title("## First\n# Second") == "First"
        assert _derive_title("# First\n## Second") == "First"

    def test_no_heading(self):
        assert _derive_title("Just text.") == ""
