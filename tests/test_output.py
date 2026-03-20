"""Tests for cce.output — serialization and file output."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cce.models.content import ContentScores
from cce.models.job import Job, JobStatus
from cce.models.package import PackageLineage, PublishPackage
from cce.orchestrator.pipeline import PipelineResult
from cce.output import serialize_result, write_output
from cce.verification.gate import GateDecision, GateResult
from tests.conftest import (
    make_content_unit,
    make_curation_request,
    make_evidence,
    make_verification_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_result(*, failed: bool = False) -> PipelineResult:
    """Build a PipelineResult for testing."""
    request = make_curation_request()

    if failed:
        job = Job(
            id="job_test",
            request=request,
            status=JobStatus.FAILED,
        )
        return PipelineResult(package=None, job=job, gate_results=[])

    job = Job(
        id="job_test",
        request=request,
        status=JobStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
    )
    unit = make_content_unit()
    evidence = [make_evidence(id="ev_001")]
    report = make_verification_report()

    gate_result = GateResult(
        decision=GateDecision.PASS,
        confidence=0.9,
        coverage=0.8,
        feedback="No issues found.",
        report=report,
        iteration=1,
    )

    package = PublishPackage(
        job_id=job.id,
        units=[unit],
        evidence=evidence,
        scores=ContentScores(confidence=0.9, coverage=0.8, source_diversity=0.7),
        lineage=PackageLineage(
            policy_id="test-policy",
            run_id="run_test123",
            engine_version="0.1.0",
        ),
    )

    return PipelineResult(package=package, job=job, gate_results=[gate_result])


# ---------------------------------------------------------------------------
# serialize_result — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_serialize_result_completed():
    result = _make_pipeline_result()
    serialized = serialize_result(result)

    assert "status" in serialized
    assert "job" in serialized
    assert "package" in serialized
    assert "gate_results" in serialized
    assert serialized["status"] == "completed"


@pytest.mark.unit
def test_serialize_result_failed():
    result = _make_pipeline_result(failed=True)
    serialized = serialize_result(result)

    assert serialized["package"] is None
    assert serialized["status"] == "failed"


@pytest.mark.unit
def test_serialize_handles_datetime():
    result = _make_pipeline_result()
    serialized = serialize_result(result)

    # Job has datetime fields — they should be ISO strings
    job_data = serialized["job"]
    if "created_at" in job_data:
        assert isinstance(job_data["created_at"], str)
    if "updated_at" in job_data:
        assert isinstance(job_data["updated_at"], str)

    # Should be JSON-serializable without errors
    json.dumps(serialized, default=str)


@pytest.mark.unit
def test_serialize_handles_enums():
    result = _make_pipeline_result()
    serialized = serialize_result(result)

    # GateDecision should be serialized as string
    assert serialized["gate_results"][0]["decision"] == "pass"
    # JobStatus should be serialized as string
    assert serialized["status"] == "completed"


@pytest.mark.unit
def test_serialize_handles_paths():
    from cce.output import _convert_value

    assert _convert_value(Path("evidence.db")) == "evidence.db"
    assert _convert_value(Path("/tmp/test/file.json")) == "/tmp/test/file.json"


# ---------------------------------------------------------------------------
# write_output — integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_output_creates_files(tmp_path):
    result = _make_pipeline_result()
    run_dir = write_output(result, tmp_path)

    assert run_dir.is_dir()
    assert (run_dir / "result.json").exists()
    assert (run_dir / "content.md").exists()
    assert (run_dir / "evidence.json").exists()
    assert (run_dir / "verification.json").exists()


@pytest.mark.integration
def test_write_output_result_json_valid(tmp_path):
    result = _make_pipeline_result()
    run_dir = write_output(result, tmp_path)

    result_json = json.loads((run_dir / "result.json").read_text())
    expected = serialize_result(result)

    assert result_json["status"] == expected["status"]
    assert result_json["package"] is not None
    assert len(result_json["gate_results"]) == len(expected["gate_results"])


@pytest.mark.integration
def test_write_output_no_package(tmp_path):
    result = _make_pipeline_result(failed=True)
    run_dir = write_output(result, tmp_path)

    assert (run_dir / "content.md").exists()
    content = (run_dir / "content.md").read_text()
    assert "(no content)" in content
