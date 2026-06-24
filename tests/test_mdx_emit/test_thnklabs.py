"""Tests for cce.output.mdx.thnklabs — thnkLabs ArticleMetadata emit format."""

from __future__ import annotations

import json

import pytest

from cce.models.content import ContentLineage, ContentScores, ContentUnit
from cce.output.mdx.thnklabs import _derive_excerpt, _read_time, format_thnklabs_page
from tests.conftest import make_evidence

pytestmark = pytest.mark.unit


def _unit(content: str, path: str = "learn", tags: list[str] | None = None) -> ContentUnit:
    return ContentUnit(
        id="cu_1",
        path=path,
        content=content,
        tags=tags or [],
        citations=[],
        evidence_map=[],
        scores=ContentScores(confidence=1.0, coverage=1.0, source_diversity=0.5),
        lineage=ContentLineage(policy_id="p", run_id="r", engine_version="0.4.0"),
    )


def _parse_metadata(mdx: str) -> dict:
    block = mdx.split("\n\n", 1)[0]
    return json.loads(block[block.index("{") : block.rindex("}") + 1])


class TestFormatThnklabsPage:
    def test_metadata_uses_thnklabs_shape(self):
        body = "# Sleep Hygiene\n\n## The Basics\n\nSleep matters [ev:ev_1] a lot."
        lookup = {"ev_1": make_evidence(id="ev_1", url="https://who.int/sleep")}
        mdx = format_thnklabs_page(
            _unit(body), lookup, topic_slug="sleep", status="published"
        )
        meta = _parse_metadata(mdx)
        assert meta["topicSlug"] == "sleep"
        assert meta["title"] == "Sleep Hygiene"
        assert meta["path"] == "learn"
        assert meta["status"] == "published"
        assert meta["excerpt"]
        assert isinstance(meta["readTime"], int) and meta["readTime"] >= 1
        assert meta["dimensions"] == []
        # optional schema fields are included (ArticleMetadata.scores?)
        assert set(meta["scores"]) == {"confidence", "coverage", "sourceDiversity"}
        # engine-internal fields must NOT appear in thnkLabs ArticleMetadata
        for k in (
            "topic",
            "curationJob",
            "policyId",
            "taxonomyId",
            "pathConfigId",
            "engineVersion",
        ):
            assert k not in meta
        assert meta["citations"][0]["url"] == "https://who.int/sleep"
        assert "[^1]" in mdx

    def test_curated_at_included_only_when_provided(self):
        with_ts = format_thnklabs_page(
            _unit("# T\n\nHi."), {}, topic_slug="t", curated_at="2026-06-23T00:00:00+00:00"
        )
        assert _parse_metadata(with_ts)["curatedAt"] == "2026-06-23T00:00:00+00:00"
        without = format_thnklabs_page(_unit("# T\n\nHi."), {}, topic_slug="t")
        assert "curatedAt" not in _parse_metadata(without)

    def test_metadata_block_closes_with_semicolon(self):
        mdx = format_thnklabs_page(_unit("# T\n\nHello world."), {}, topic_slug="t")
        assert mdx.split("\n\n", 1)[0].rstrip().endswith("};")

    def test_status_defaults_to_draft(self):
        mdx = format_thnklabs_page(_unit("# T\n\nHi."), {}, topic_slug="t")
        assert _parse_metadata(mdx)["status"] == "draft"

    def test_no_citations_key_when_none_used(self):
        mdx = format_thnklabs_page(_unit("# T\n\nNo sources here."), {}, topic_slug="t")
        assert "citations" not in _parse_metadata(mdx)

    def test_resources_section_rebuilt_grounded(self):
        body = (
            "# T\n\n## Physical Well-Being\n\nLoneliness raises risk [ev:ev_1].\n\n"
            "## Curated Resources\n\n"
            "- **Real Source** [ev:ev_1]: a grounded source\n"
            "- **Invented Podcast — Someone**: recalled, not in evidence\n"
        )
        lookup = {"ev_1": make_evidence(id="ev_1", url="https://who.int/x", title="WHO X")}
        mdx = format_thnklabs_page(_unit(body), lookup, topic_slug="t")
        res = mdx.split("## Curated Resources", 1)[1]
        bullets = [ln for ln in res.splitlines() if ln.strip().startswith("-")]
        assert bullets, "rebuilt resources section has bullets"
        assert all("[^" in b for b in bullets), "every resource bullet is cited"
        assert "Invented Podcast" not in mdx, "ungrounded source dropped"
        assert "WHO X" in res, "grounded cited source listed"


class TestHelpers:
    def test_read_time_rounds_on_200_wpm(self):
        assert _read_time(" ".join(["w"] * 1628)) == 8  # deployed loneliness/learn
        assert _read_time(" ".join(["w"] * 777)) == 4  # deployed loneliness/apply
        assert _read_time("one two three") == 1  # floor

    def test_excerpt_skips_headings_strips_markers_truncates(self):
        body = "# Title\n\n## Heading\n\n" + "word " * 60 + "[^1] end."
        ex = _derive_excerpt(body, limit=100)
        assert not ex.startswith("#")
        assert "[^1]" not in ex
        assert ex.endswith("…")
        assert len(ex) <= 102

    def test_excerpt_short_paragraph_returned_verbatim(self):
        assert _derive_excerpt("# T\n\nShort intro.") == "Short intro."
