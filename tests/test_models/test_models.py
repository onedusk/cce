"""Tests for CCE Pydantic model constraints — frozen, bounds, defaults."""

import pytest
from pydantic import ValidationError

from cce.models.content import ContentScores
from cce.models.evidence import Evidence, SourceQuality
from cce.models.job import JobStatus
from cce.models.request import CurationRequest
from tests.conftest import make_evidence

pytestmark = pytest.mark.unit


def test_evidence_frozen():
    ev = make_evidence()
    with pytest.raises(ValidationError):
        ev.url = "https://changed.com"


def test_source_quality_frozen():
    sq = SourceQuality(
        is_peer_reviewed=True,
        is_primary_source=False,
        domain_reputation="trusted",
        conflict_of_interest=False,
    )
    with pytest.raises(ValidationError):
        sq.is_peer_reviewed = False


def test_content_scores_bounds():
    with pytest.raises(ValidationError):
        ContentScores(confidence=-0.1, coverage=0.0, source_diversity=0.0)
    with pytest.raises(ValidationError):
        ContentScores(confidence=1.1, coverage=0.0, source_diversity=0.0)
    with pytest.raises(ValidationError):
        ContentScores(confidence=0.5, coverage=-0.01, source_diversity=0.0)
    with pytest.raises(ValidationError):
        ContentScores(confidence=0.5, coverage=0.0, source_diversity=1.01)
    # Valid boundary values should pass
    scores = ContentScores(confidence=0.0, coverage=1.0, source_diversity=0.5)
    assert scores.confidence == 0.0
    assert scores.coverage == 1.0


def test_job_status_enum_values():
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.RUNNING.value == "running"
    assert JobStatus.COMPLETED.value == "completed"
    assert JobStatus.FAILED.value == "failed"
    assert JobStatus.CANCELLED.value == "cancelled"
    assert JobStatus.REVIEW_REQUIRED.value == "review_required"


def test_curation_request_defaults():
    req = CurationRequest(topic="x", paths=["a"], policy_id="p")
    assert req.audience == "general"
    assert req.risk_profile == "medium"
    assert req.subtopics == []
    assert req.constraints is None
