"""Shared fixtures for MDX emit tests."""

from __future__ import annotations

import pytest

from cce.models.content import Citation, ContentScores
from cce.models.evidence import Evidence
from cce.models.package import PackageLineage, PublishPackage
from tests.conftest import make_content_unit, make_evidence, make_publish_package


@pytest.fixture
def sample_evidence_pair() -> list[Evidence]:
    """Two evidence objects with known IDs for citation testing."""
    return [
        make_evidence(
            id="ev_test_001",
            url="https://example.com/source-1",
            title="Source One",
            author="Dr. Alpha",
        ),
        make_evidence(
            id="ev_test_002",
            url="https://example.com/source-2",
            title="Source Two",
            author="Dr. Beta",
        ),
    ]


@pytest.fixture
def evidence_lookup(sample_evidence_pair: list[Evidence]) -> dict[str, Evidence]:
    """Evidence-by-ID dict built from sample_evidence_pair."""
    return {ev.id: ev for ev in sample_evidence_pair}


@pytest.fixture
def sample_package(sample_evidence_pair: list[Evidence]) -> PublishPackage:
    """Package with learn + explore units citing both sample evidence objects."""
    learn = make_content_unit(
        path="learn",
        content="## Learn\n\nClaim one [ev:ev_test_001] and two [ev:ev_test_002].",
        citations=[
            Citation(evidence_id="ev_test_001", url="https://example.com/source-1"),
            Citation(evidence_id="ev_test_002", url="https://example.com/source-2"),
        ],
        tags=["emotional"],
        scores=ContentScores(confidence=0.9, coverage=0.85, source_diversity=0.8),
    )
    explore = make_content_unit(
        path="explore",
        content="## Explore\n\nMore claims [ev:ev_test_001].",
        citations=[
            Citation(evidence_id="ev_test_001", url="https://example.com/source-1"),
        ],
        tags=["physical"],
        scores=ContentScores(confidence=0.88, coverage=0.82, source_diversity=0.75),
    )
    return make_publish_package(
        units=[learn, explore],
        evidence=sample_evidence_pair,
        scores=ContentScores(confidence=0.89, coverage=0.84, source_diversity=0.78),
    )
