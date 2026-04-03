"""Tests for cce.output.mdx.evidence — evidence JSON exporter."""

from __future__ import annotations

import json

from cce.models.content import Citation
from cce.models.evidence import SourceQuality
from cce.output.mdx.evidence import export_evidence
from tests.conftest import make_content_unit, make_evidence


def _unit_with_citations(path: str, *evidence_ids: str):
    """Build a ContentUnit with citations for the given evidence IDs."""
    return make_content_unit(
        path=path,
        citations=[
            Citation(evidence_id=eid, url=f"https://example.com/{eid}")
            for eid in evidence_ids
        ],
    )


class TestExportEvidence:
    def test_single_unit_single_evidence(self):
        ev = make_evidence(id="ev_1")
        unit = _unit_with_citations("learn", "ev_1")

        result = json.loads(export_evidence([unit], {"ev_1": ev}))

        assert len(result) == 1
        assert result[0]["id"] == "ev_1"

    def test_deduplication_across_paths(self):
        ev = make_evidence(id="ev_shared")
        learn = _unit_with_citations("learn", "ev_shared")
        explore = _unit_with_citations("explore", "ev_shared")

        result = json.loads(export_evidence([learn, explore], {"ev_shared": ev}))

        assert len(result) == 1

    def test_ordering_by_path(self):
        ev_a = make_evidence(id="ev_apply_only")
        ev_l = make_evidence(id="ev_learn_only")
        apply_unit = _unit_with_citations("apply", "ev_apply_only")
        learn_unit = _unit_with_citations("learn", "ev_learn_only")

        # Pass in reverse order — exporter should sort by path_order
        result = json.loads(
            export_evidence([apply_unit, learn_unit], {
                "ev_apply_only": ev_a,
                "ev_learn_only": ev_l,
            })
        )

        assert result[0]["id"] == "ev_learn_only"
        assert result[1]["id"] == "ev_apply_only"

    def test_all_fields_serialized(self):
        ev = make_evidence(
            id="ev_full",
            source_quality=SourceQuality(
                is_peer_reviewed=True,
                is_primary_source=False,
                domain_reputation="trusted",
                conflict_of_interest=False,
            ),
            tags=["emotional"],
            dimension_signals={"emotional": "primary"},
        )
        unit = _unit_with_citations("learn", "ev_full")

        result = json.loads(export_evidence([unit], {"ev_full": ev}))

        entry = result[0]
        assert entry["sourceQuality"]["peer_reviewed"] is True
        assert entry["sourceQuality"]["domain_reputation"] == "trusted"
        assert entry["tags"] == ["emotional"]
        assert entry["dimensionSignals"] == {"emotional": "primary"}
        assert "retrievedAt" in entry
        assert "excerpt" in entry

    def test_optional_fields_omitted(self):
        ev = make_evidence(id="ev_bare", title=None, author=None, published_at=None)
        unit = _unit_with_citations("learn", "ev_bare")

        result = json.loads(export_evidence([unit], {"ev_bare": ev}))

        entry = result[0]
        assert "title" not in entry
        assert "author" not in entry
        assert "publishedAt" not in entry

    def test_empty_units(self):
        result = json.loads(export_evidence([], {}))
        assert result == []

    def test_unknown_path_sorted_last(self):
        ev_c = make_evidence(id="ev_custom")
        ev_l = make_evidence(id="ev_learn")
        custom_unit = _unit_with_citations("custom", "ev_custom")
        learn_unit = _unit_with_citations("learn", "ev_learn")

        result = json.loads(
            export_evidence([custom_unit, learn_unit], {
                "ev_custom": ev_c,
                "ev_learn": ev_l,
            })
        )

        assert result[0]["id"] == "ev_learn"
        assert result[1]["id"] == "ev_custom"

    def test_datetimes_are_iso_strings(self):
        ev = make_evidence(id="ev_dt")
        unit = _unit_with_citations("learn", "ev_dt")

        result = json.loads(export_evidence([unit], {"ev_dt": ev}))

        # Should be ISO string, not Python repr
        assert isinstance(result[0]["retrievedAt"], str)
        assert "T" in result[0]["retrievedAt"]
