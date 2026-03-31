"""Unit tests for job endpoints — HTTP layer only, no pipeline execution."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cce.models.job import JobStatus
from tests.conftest import make_job, make_publish_package


pytestmark = pytest.mark.unit


async def test_create_job_returns_202(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/curate/jobs",
        json={
            "topic": "test topic",
            "paths": ["blog"],
            "policy_id": "test-policy",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["data"]["status"] == "queued"
    assert body["data"]["id"].startswith("job_")
    assert body["data"]["topic"] == "test topic"


async def test_create_job_invalid_policy_returns_404(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/curate/jobs",
        json={
            "topic": "test",
            "paths": ["blog"],
            "policy_id": "nonexistent-policy",
        },
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"].lower()


async def test_get_job_after_create(client: httpx.AsyncClient, app):
    # Create a job
    resp = await client.post(
        "/v1/curate/jobs",
        json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
    )
    job_id = resp.json()["data"]["id"]

    # Give background task a chance to start (it will fail since mock LLM has no responses)
    await asyncio.sleep(0.05)

    # GET should return the job
    resp = await client.get(f"/v1/curate/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == job_id


async def test_get_nonexistent_job_returns_404(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/jobs/nonexistent")
    assert resp.status_code == 404


async def test_list_jobs(client: httpx.AsyncClient):
    # Create two jobs
    for _ in range(2):
        await client.post(
            "/v1/curate/jobs",
            json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
        )

    resp = await client.get("/v1/curate/jobs")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 2
    assert len(data["jobs"]) >= 2
    assert "limit" in data
    assert "offset" in data


async def test_list_jobs_filter_by_status(client: httpx.AsyncClient):
    # Create a job (starts as queued, background task may change it)
    await client.post(
        "/v1/curate/jobs",
        json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
    )

    resp = await client.get("/v1/curate/jobs?status=queued")
    assert resp.status_code == 200
    for job in resp.json()["data"]["jobs"]:
        assert job["status"] == "queued"


async def test_list_jobs_invalid_status_returns_400(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/jobs?status=invalid_status")
    assert resp.status_code == 400


async def test_delete_job(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/curate/jobs",
        json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
    )
    job_id = resp.json()["data"]["id"]

    # Wait briefly for task to register
    await asyncio.sleep(0.05)

    resp = await client.delete(f"/v1/curate/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == job_id

    # Verify it's gone
    resp = await client.get(f"/v1/curate/jobs/{job_id}")
    assert resp.status_code == 404


async def test_delete_nonexistent_returns_404(client: httpx.AsyncClient):
    resp = await client.delete("/v1/curate/jobs/nonexistent")
    assert resp.status_code == 404


async def test_get_package_not_found(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/jobs/nonexistent/package")
    assert resp.status_code == 404


async def test_retry_nonexistent_returns_404(client: httpx.AsyncClient):
    resp = await client.post("/v1/curate/jobs/nonexistent/retry")
    assert resp.status_code == 404
