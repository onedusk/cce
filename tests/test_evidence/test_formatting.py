"""Tests for shared evidence formatting."""

from __future__ import annotations

import pytest

from cce.evidence.formatting import format_evidence_for_prompt
from cce.models.evidence import SourceQuality
from tests.conftest import make_evidence

pytestmark = pytest.mark.unit


class TestFormatEvidenceForPrompt:
    def test_writer_style_full_metadata(self):
        ev = make_evidence(
            id="ev_001", url="https://example.com", title="Title", author="Author"
        )
        result = format_evidence_for_prompt([ev], style="writer")

        assert "--- EVIDENCE [ev_001] ---" in result
        assert "URL: https://example.com" in result
        assert "Title: Title" in result
        assert "Author: Author" in result
        assert ev.excerpt in result

    def test_verifier_style_compact(self):
        ev = make_evidence(id="ev_001", url="https://example.com")
        result = format_evidence_for_prompt([ev], style="verifier")

        assert "[ev_001] (URL: https://example.com)" in result
        assert ev.excerpt in result
        # Verifier style doesn't include title/author
        assert "Title:" not in result

    def test_empty_evidence_list(self):
        assert format_evidence_for_prompt([], style="writer") == ""
        assert format_evidence_for_prompt([], style="verifier") == ""

    def test_writer_no_quality_signals(self):
        ev = make_evidence(id="ev_bare", source_quality=None)
        result = format_evidence_for_prompt([ev], style="writer")

        assert "Reputation:" not in result
        assert "Type:" not in result

    def test_writer_peer_reviewed(self):
        ev = make_evidence(source_quality=SourceQuality(is_peer_reviewed=True))
        result = format_evidence_for_prompt([ev], style="writer")
        assert "Type: peer-reviewed" in result

    def test_verifier_peer_reviewed_tag(self):
        ev = make_evidence(source_quality=SourceQuality(is_peer_reviewed=True))
        result = format_evidence_for_prompt([ev], style="verifier")
        assert "[peer-reviewed]" in result

    def test_verifier_coi_tag(self):
        ev = make_evidence(source_quality=SourceQuality(conflict_of_interest=True))
        result = format_evidence_for_prompt([ev], style="verifier")
        assert "[potential-COI]" in result

    def test_default_style_is_writer(self):
        ev = make_evidence(id="ev_default")
        result = format_evidence_for_prompt([ev])
        assert "--- EVIDENCE [ev_default] ---" in result
