"""Tests for CCE Pydantic model constraints — frozen, bounds, defaults."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cce.models.content import ContentScores, ContentUnit
from cce.models.evidence import DiscoveryResult, SourceQuality
from cce.models.job import JobStage, JobStatus, StageRecord
from cce.models.package import PublishPackage
from cce.models.request import CurationRequest
from cce.models.style import StyleScores
from tests.conftest import make_content_unit, make_evidence, make_publish_package

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


# ---------------------------------------------------------------------------
# M07 additions: DiscoveryResult, draft_source, with_scores helpers (T-07.06)
# ---------------------------------------------------------------------------


def test_discovery_result_frozen_with_defaults():
    result = DiscoveryResult()
    assert result.evidence == []
    assert result.metrics == {}
    with pytest.raises(ValidationError):
        result.metrics = {"crawl_success": 1}  # type: ignore[misc]


def test_content_unit_draft_source_defaults_to_writer():
    assert make_content_unit().draft_source == "writer"


def test_content_unit_draft_source_rejects_unknown_agent():
    with pytest.raises(ValidationError):
        make_content_unit(draft_source="critic")


def test_content_unit_with_scores_round_trip():
    """with_scores returns a new frozen unit with only `scores` replaced;
    the original is untouched (immutability round-trip)."""
    unit = make_content_unit()
    new_scores = ContentScores(confidence=0.9, coverage=0.8, source_diversity=0.7)

    updated = unit.with_scores(new_scores)

    assert updated.scores == new_scores
    assert updated.id == unit.id
    assert updated.content == unit.content
    assert updated.draft_source == unit.draft_source
    # Original unchanged — frozen-copy semantics
    assert unit.scores.confidence == 0.0
    # Round-trip survives serialization
    assert ContentUnit.model_validate(updated.model_dump()).scores == new_scores


def test_content_unit_with_style_scores_round_trip():
    unit = make_content_unit()
    style = StyleScores(
        sentence_length_stddev=12.5,
        suppressed_vocab_hits=1,
        type_token_ratio=0.7,
        formulaic_transition_count=0,
        contrastive_frame_count=0,
        hedging_phrase_count=0,
        em_dash_count=2,
        word_count=250,
        humanization_pass=True,
    )

    updated = unit.with_style_scores(style)

    assert updated.style_scores == style
    assert updated.scores == unit.scores
    assert unit.style_scores is None  # original untouched


def test_pre_m07_stored_package_json_still_parses():
    """Backward compat (T-07.05): a package stored before draft_source /
    StageRecord.path existed must parse, with the new fields defaulting."""
    unit_dict = make_content_unit().model_dump(mode="json")
    unit_dict.pop("draft_source")  # field did not exist pre-M07

    rec_dict = StageRecord(
        stage=JobStage.WRITE,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        metrics={"path": "blog", "iterations": 1},
    ).model_dump(mode="json")
    rec_dict.pop("path")  # field did not exist pre-M07

    package_dict = make_publish_package().model_dump(mode="json")
    package_dict["units"] = [unit_dict]
    package_dict["lineage"]["stages"] = [rec_dict]

    package = PublishPackage.model_validate(package_dict)

    assert package.units[0].draft_source == "writer"
    assert package.lineage.stages[0].path is None
