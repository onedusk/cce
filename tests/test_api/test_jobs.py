"""Unit tests for job endpoints — HTTP layer only, no pipeline execution."""

from __future__ import annotations

import httpx
import pytest

from tests.test_api.conftest import wait_for_job_status

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
    err = resp.json()["error"]
    assert err["code"] == "policy_not_found"
    assert "not found" in err["message"].lower()


async def test_get_job_after_create(client: httpx.AsyncClient, app):
    # Create a job
    resp = await client.post(
        "/v1/curate/jobs",
        json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
    )
    job_id = resp.json()["data"]["id"]

    # Wait for the background task to reach a terminal state (it fails since
    # the mock LLM has no responses) — poll-with-deadline, not a fixed sleep.
    await wait_for_job_status(client, job_id, {"failed", "completed"})

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

    # Wait for the background task to reach a terminal state — poll-with-
    # deadline, not a fixed sleep (the mock LLM makes the job fail fast).
    await wait_for_job_status(client, job_id, {"failed", "completed"})

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
