"""Tests for cce.output.mdx.citations — citation index builder."""

from __future__ import annotations

import json
import re
from datetime import UTC

import pytest

from cce.models.content import Citation, ClaimMapping
from cce.output.mdx.citations import build_citation_index
from cce.output.mdx.formatter import format_mdx_page
from tests.conftest import make_content_unit, make_evidence


def _make_lookup(*ids: str) -> dict:
    """Build an evidence_by_id dict with known IDs."""
    return {eid: make_evidence(id=eid, url=f"https://example.com/{eid}") for eid in ids}


class TestBuildCitationIndex:
    pytestmark = pytest.mark.unit

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


class TestUrlKeyedDedup:
    """M02: footnote indices are keyed on canonical source URL, not evidence id.

    Multiple distinct evidence excerpts of the same page collapse onto one
    footnote number (one number per source URL, per article — ADR-001/002).
    """

    pytestmark = pytest.mark.unit

    def test_same_url_different_ids_one_entry(self):
        """Two distinct evidence IDs sharing one URL produce a SINGLE entry,
        and both markers render the same [^1]."""
        lookup = {
            "ev_a": make_evidence(id="ev_a", url="https://who.int/x"),
            "ev_b": make_evidence(id="ev_b", url="https://who.int/x"),
        }
        result = build_citation_index(
            "First excerpt [ev:ev_a] and second excerpt [ev:ev_b].", lookup
        )

        assert result.content == "First excerpt [^1] and second excerpt [^1]."
        assert len(result.citations) == 1
        assert result.citations[0].index == 1

    def test_distinct_urls_still_get_distinct_contiguous_indices(self):
        """Regression: distinct URLs keep distinct, contiguous indices in
        first-appearance order (the M01 distinct-URL path must not change)."""
        lookup = {
            "ev_a": make_evidence(id="ev_a", url="https://who.int/a"),
            "ev_b": make_evidence(id="ev_b", url="https://cdc.gov/b"),
            "ev_c": make_evidence(id="ev_c", url="https://nih.gov/c"),
        }
        result = build_citation_index(
            "One [ev:ev_a] two [ev:ev_b] three [ev:ev_c].", lookup
        )

        assert result.content == "One [^1] two [^2] three [^3]."
        assert [c.index for c in result.citations] == [1, 2, 3]
        assert [c.url for c in result.citations] == [
            "https://who.int/a",
            "https://cdc.gov/b",
            "https://nih.gov/c",
        ]

    def test_canonical_variants_collapse_to_one_index(self):
        """Trailing-slash and fragment variants of the same URL collapse to a
        single citation index; query strings are deliberately left distinct."""
        lookup = {
            "ev_a": make_evidence(id="ev_a", url="https://who.int/x"),
            "ev_b": make_evidence(id="ev_b", url="https://who.int/x/"),
            "ev_c": make_evidence(id="ev_c", url="https://who.int/x#frag"),
        }
        result = build_citation_index("A [ev:ev_a] B [ev:ev_b] C [ev:ev_c].", lookup)

        assert result.content == "A [^1] B [^1] C [^1]."
        assert len(result.citations) == 1
        assert result.citations[0].index == 1

    def test_representative_id_is_first_seen(self):
        """The citation entry's `id` is the first evidence id seen in the body
        for that URL (the regex substitution runs left-to-right)."""
        lookup = {
            "ev_a": make_evidence(id="ev_a", url="https://who.int/x"),
            "ev_b": make_evidence(id="ev_b", url="https://who.int/x"),
        }
        result = build_citation_index("Cite [ev:ev_b]... then [ev:ev_a].", lookup)

        assert len(result.citations) == 1
        # ev_b appears first in the body, so it is the representative id.
        assert result.citations[0].id == "ev_b"

    def test_unknown_id_still_question_mark_with_url_keying(self):
        """[^?] is still emitted for unknown IDs even with URL-keyed dedup."""
        lookup = {"ev_real": make_evidence(id="ev_real", url="https://who.int/x")}
        result = build_citation_index(
            "Real [ev:ev_real] and missing [ev:ev_gone].", lookup
        )

        assert result.content == "Real [^1] and missing [^?]."
        assert len(result.citations) == 1


class TestEmitInvariant:
    """T-02.03: stored-job re-emit collapses same-URL citations at render time
    only — the ContentUnit's stored claim->evidence mapping is untouched."""

    pytestmark = pytest.mark.integration

    @staticmethod
    def _parse_page(mdx: str) -> tuple[dict, str]:
        """Split a page.mdx string into (metadata dict, body)."""
        prefix = "export const metadata = "
        assert mdx.startswith(prefix)
        metadata, end = json.JSONDecoder().raw_decode(mdx[len(prefix) :])
        return metadata, mdx[len(prefix) + end :].strip()

    def test_same_url_via_three_ids_yields_one_metadata_citation(self):
        """A unit citing one URL through 3 evidence IDs (plus a distinct second
        URL) emits one metadata.citations entry per URL with contiguous 1..N."""
        ev_a = make_evidence(id="ev_a", url="https://who.int/loneliness")
        ev_b = make_evidence(id="ev_b", url="https://who.int/loneliness")
        ev_c = make_evidence(id="ev_c", url="https://who.int/loneliness")
        ev_d = make_evidence(id="ev_d", url="https://cdc.gov/data")
        evidence_by_id = {e.id: e for e in (ev_a, ev_b, ev_c, ev_d)}

        unit = make_content_unit(
            path="learn",
            content=(
                "## Loneliness\n\n"
                "First [ev:ev_a], second [ev:ev_b], third [ev:ev_c] all from one "
                "source. A distinct source says more [ev:ev_d]."
            ),
            citations=[
                Citation(evidence_id="ev_a", url="https://who.int/loneliness"),
                Citation(evidence_id="ev_b", url="https://who.int/loneliness"),
                Citation(evidence_id="ev_c", url="https://who.int/loneliness"),
                Citation(evidence_id="ev_d", url="https://cdc.gov/data"),
            ],
            evidence_map=[
                ClaimMapping(claim="A claim", evidence_ids=["ev_a", "ev_b", "ev_c"]),
                ClaimMapping(claim="Another claim", evidence_ids=["ev_d"]),
            ],
        )

        mdx = format_mdx_page(unit, evidence_by_id, job_id="job-1")
        metadata, body = self._parse_page(mdx)

        # One metadata citation entry per unique URL.
        assert len(metadata["citations"]) == 2
        assert {c["url"] for c in metadata["citations"]} == {
            "https://who.int/loneliness",
            "https://cdc.gov/data",
        }
        # Citation indices contiguous 1..N.
        assert [c["index"] for c in metadata["citations"]] == [1, 2]
        # Body footnote markers are contiguous 1..N with no gaps.
        body_indices = sorted({int(m) for m in re.findall(r"\[\^(\d+)\]", body)})
        assert body_indices == [1, 2]
        # The three same-URL markers all collapsed to [^1].
        assert body.count("[^1]") == 3
        assert body.count("[^2]") == 1

    def test_emit_does_not_mutate_stored_citations_or_evidence_map(self):
        """De-dup is render-only: the input ContentUnit's per-evidence
        citations and claim->evidence map are unchanged after emit (Stage 0
        invariant — 'no citation, no ship' relies on this granularity)."""
        ev_a = make_evidence(id="ev_a", url="https://who.int/loneliness")
        ev_b = make_evidence(id="ev_b", url="https://who.int/loneliness")
        ev_c = make_evidence(id="ev_c", url="https://who.int/loneliness")
        evidence_by_id = {e.id: e for e in (ev_a, ev_b, ev_c)}

        unit = make_content_unit(
            path="learn",
            content="One [ev:ev_a] two [ev:ev_b] three [ev:ev_c].",
            citations=[
                Citation(evidence_id="ev_a", url="https://who.int/loneliness"),
                Citation(evidence_id="ev_b", url="https://who.int/loneliness"),
                Citation(evidence_id="ev_c", url="https://who.int/loneliness"),
            ],
            evidence_map=[
                ClaimMapping(
                    claim="One source, three excerpts",
                    evidence_ids=["ev_a", "ev_b", "ev_c"],
                ),
            ],
        )
        # Snapshot the stored claim->evidence mapping before emit.
        pre_citations = list(unit.citations)
        pre_evidence_map = list(unit.evidence_map)

        mdx = format_mdx_page(unit, evidence_by_id, job_id="job-1")
        metadata, _ = self._parse_page(mdx)

        # Display de-dup collapsed three same-URL excerpts to one footnote...
        assert len(metadata["citations"]) == 1
        # ...while the stored per-evidence granularity is fully preserved.
        assert unit.citations == pre_citations
        assert len(unit.citations) == 3
        assert unit.evidence_map == pre_evidence_map
        assert unit.evidence_map[0].evidence_ids == ["ev_a", "ev_b", "ev_c"]
