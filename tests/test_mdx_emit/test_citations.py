"""Tests for cce.output.mdx.citations — citation index builder."""

from __future__ import annotations

from datetime import UTC

from cce.output.mdx.citations import build_citation_index
from tests.conftest import make_evidence


def _make_lookup(*ids: str) -> dict:
    """Build an evidence_by_id dict with known IDs."""
    return {eid: make_evidence(id=eid, url=f"https://example.com/{eid}") for eid in ids}


class TestBuildCitationIndex:
    def test_single_citation(self):
        lookup = _make_lookup("ev_001")
        result = build_citation_index("A claim [ev:ev_001].", lookup)

        assert result.content == "A claim [^1]."
        assert len(result.citations) == 1
        assert result.citations[0].id == "ev_001"
        assert result.citations[0].index == 1

    def test_multiple_citations_ordered_by_appearance(self):
        lookup = _make_lookup("ev_a", "ev_b", "ev_c")
        result = build_citation_index(
            "First [ev:ev_b] second [ev:ev_c] third [ev:ev_a].", lookup
        )

        assert result.content == "First [^1] second [^2] third [^3]."
        assert [c.id for c in result.citations] == ["ev_b", "ev_c", "ev_a"]
        assert [c.index for c in result.citations] == [1, 2, 3]

    def test_repeated_id_reuses_index(self):
        lookup = _make_lookup("ev_001")
        result = build_citation_index(
            "Claim [ev:ev_001] and again [ev:ev_001].", lookup
        )

        assert result.content == "Claim [^1] and again [^1]."
        assert len(result.citations) == 1

    def test_missing_evidence_becomes_question_mark(self):
        result = build_citation_index("Missing [ev:ev_gone].", {})

        assert result.content == "Missing [^?]."
        assert len(result.citations) == 0

    def test_mixed_present_and_missing(self):
        lookup = _make_lookup("ev_real")
        result = build_citation_index(
            "Real [ev:ev_real] and missing [ev:ev_nope].", lookup
        )

        assert result.content == "Real [^1] and missing [^?]."
        assert len(result.citations) == 1
        assert result.citations[0].id == "ev_real"

    def test_empty_content(self):
        result = build_citation_index("", {})

        assert result.content == ""
        assert result.citations == ()

    def test_no_markers(self):
        result = build_citation_index("Plain text with no citations.", {})

        assert result.content == "Plain text with no citations."
        assert result.citations == ()

    def test_citation_fields_match_evidence(self):
        from datetime import datetime

        ev = make_evidence(
            id="ev_x",
            url="https://example.com/paper",
            title="A Paper",
            author="Dr. Smith",
            published_at=datetime(2024, 6, 15, tzinfo=UTC),
        )
        result = build_citation_index("[ev:ev_x]", {"ev_x": ev})

        c = result.citations[0]
        assert c.url == "https://example.com/paper"
        assert c.title == "A Paper"
        assert c.author == "Dr. Smith"
        assert c.published_at == "2024-06-15T00:00:00+00:00"

    def test_evidence_without_published_at(self):
        ev = make_evidence(id="ev_np", published_at=None)
        result = build_citation_index("[ev:ev_np]", {"ev_np": ev})

        assert result.citations[0].published_at is None

    def test_bare_format_without_colon(self):
        """LLM often outputs [ev_abc123] instead of [ev:ev_abc123]."""
        lookup = _make_lookup("ev_abc123")
        result = build_citation_index("A claim [ev_abc123].", lookup)

        assert result.content == "A claim [^1]."
        assert len(result.citations) == 1
        assert result.citations[0].id == "ev_abc123"

    def test_mixed_colon_and_bare_formats(self):
        lookup = _make_lookup("ev_a", "ev_b")
        result = build_citation_index("Colon [ev:ev_a] and bare [ev_b].", lookup)

        assert result.content == "Colon [^1] and bare [^2]."
        assert len(result.citations) == 2

    def test_writer_drops_ev_prefix_in_marker(self):
        """Writer prompt says [ev:EVIDENCE_ID] but evidence block shows IDs as
        [ev_HASH]. The LLM frequently interprets EVIDENCE_ID as just the HASH
        and emits [ev:HASH] without the ev_ prefix. Regression: this caused
        every learn-path footnote to render as [^?] in the loneliness rewrite
        run. Lookup must transparently retry with `ev_` prepended."""
        # Evidence is stored with the canonical `ev_` prefix:
        lookup = _make_lookup("ev_abc123", "ev_xyz789")
        # Writer cited without the prefix:
        result = build_citation_index(
            "First claim [ev:abc123] and second [ev:xyz789].", lookup
        )

        assert result.content == "First claim [^1] and second [^2]."
        assert "[^?]" not in result.content
        assert len(result.citations) == 2
        # Citations should be canonicalized to the `ev_`-prefixed form
        # so downstream consumers see one form, not a mix.
        assert {c.id for c in result.citations} == {"ev_abc123", "ev_xyz789"}

    def test_unprefixed_marker_repeated_collapses_to_one_index(self):
        """A repeated [ev:HASH] marker (no `ev_` prefix) should reuse the
        same footnote index across all occurrences — not allocate a new one."""
        lookup = _make_lookup("ev_abc")
        result = build_citation_index(
            "First [ev:abc] and again [ev:abc] and third time [ev:abc].", lookup
        )

        assert result.content == "First [^1] and again [^1] and third time [^1]."
        assert len(result.citations) == 1
