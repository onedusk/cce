"""Smoke tests for the FastAPI app scaffold."""

from __future__ import annotations

import httpx
import pytest

from cce.api.schemas import (
    APIEnvelope,
    JobCreateRequest,
    JobResponse,
    envelope,
    job_to_response,
)
from tests.conftest import make_job

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------


async def test_app_starts(client: httpx.AsyncClient):
    """App should start and return 404 for unknown paths (no endpoints yet)."""
    resp = await client.get("/nonexistent")
    assert resp.status_code == 404


async def test_app_state_populated(app):
    """After lifespan startup, app.state should have all expected attributes."""
    assert app.state.pipeline is not None
    assert app.state.job_store is not None
    assert app.state.evidence_store is not None
    assert app.state.policies is not None
    assert app.state.semaphore is not None
    assert isinstance(app.state.running_tasks, dict)
    assert app.state.auth_dependency is not None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_envelope_has_timestamp():
    env = envelope(data={"id": "test"})
    assert "timestamp" in env.meta
    assert env.data == {"id": "test"}
    assert env.error is None


def test_envelope_with_error():
    from cce.api.schemas import error_envelope

    env = error_envelope(code="something_failed", message="Something failed")
    assert env.error is not None
    assert env.error.code == "something_failed"
    assert env.error.message == "Something failed"
    assert env.data is None


def test_job_to_response():
    job = make_job(id="job_conv")
    resp = job_to_response(job)
    assert isinstance(resp, JobResponse)
    assert resp.id == "job_conv"
    assert resp.status == "queued"
    assert resp.topic == job.request.topic


def test_job_create_request_validates():
    req = JobCreateRequest(
        topic="test",
        paths=["learn"],
        policy_id="peer-reviewed",
    )
    assert req.audience == "general"
    assert req.risk_profile == "medium"
    assert req.subtopics == []


def test_api_envelope_serializable():
    env = APIEnvelope(data={"key": "value"})
    d = env.model_dump()
    assert d["data"] == {"key": "value"}
    assert d["error"] is None
