"""POST /v1/curate/jobs/{id}/retry lifecycle (COR-02, OPS-04)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cce.engine import CurationEngine, JobHandle
from cce.models.job import Job, JobStatus
from cce.models.request import CurationRequest
from tests.test_api.conftest import wait_for_job_status
from tests.test_api.test_jobs_lifecycle import _make_lifecycle_app

pytestmark = pytest.mark.integration

_BODY = {"topic": "test topic", "paths": ["blog"], "policy_id": "test-policy"}


async def test_failed_retry_does_not_serve_the_previous_package(
    tmp_path: Path, monkeypatch
):
    """COR-02: GET /package returns 404 while the retry runs and after it fails."""
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            job_id = (await client.post("/v1/curate/jobs", json=_BODY)).json()["data"][
                "id"
            ]
            data = await wait_for_job_status(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"
            resp = await client.get(f"/v1/curate/jobs/{job_id}/package")
            assert resp.status_code == 200

            release = asyncio.Event()

            async def _fail_later(request, policy, **kwargs):
                await release.wait()
                raise RuntimeError("retry failed")

            monkeypatch.setattr(app.state.pipeline, "run", _fail_later)

            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry")
            assert resp.status_code == 202
            resp = await client.get(f"/v1/curate/jobs/{job_id}/package")
            assert resp.status_code == 404
            stored = await job_store.get_job(job_id)
            assert stored is not None
            assert stored.stages == []

            release.set()
            data = await wait_for_job_status(client, job_id, {"failed"})
            resp = await client.get(f"/v1/curate/jobs/{job_id}/package")
            assert resp.status_code == 404

    await job_store.close()
    await evidence_store.close()


async def test_retry_with_unknown_policy_leaves_job_untouched(tmp_path: Path):
    """A 404 policy_not_found writes nothing: the job keeps its status and
    package instead of being left QUEUED with no task."""
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            job_id = (await client.post("/v1/curate/jobs", json=_BODY)).json()["data"][
                "id"
            ]
            await wait_for_job_status(client, job_id, {"completed"})

            app.state.policies.clear()
            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry")
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "policy_not_found"

            data = (await client.get(f"/v1/curate/jobs/{job_id}")).json()["data"]
            assert data["status"] == "completed"
            resp = await client.get(f"/v1/curate/jobs/{job_id}/package")
            assert resp.status_code == 200

    await job_store.close()
    await evidence_store.close()


# ---------------------------------------------------------------------------
# OPS-04: a job a crash left QUEUED or RUNNING
# ---------------------------------------------------------------------------


async def _store_orphan(job_store, status: JobStatus) -> str:
    """Store a job in ``status`` with no task behind it, as after a crash."""
    request = CurationRequest(
        topic="test topic", paths=["blog"], policy_id="test-policy"
    )
    job = Job(id="job_orphan000001", request=request, status=status)
    await job_store.create_job(job)
    return job.id


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.QUEUED])
async def test_forced_retry_recovers_an_orphaned_job(tmp_path: Path, status: JobStatus):
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            job_id = await _store_orphan(job_store, status)

            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "already_running"

            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry?force=true")
            assert resp.status_code == 202
            assert resp.json()["data"]["status"] == "queued"
            data = await wait_for_job_status(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"

    await job_store.close()
    await evidence_store.close()


async def test_forced_retry_refuses_a_job_with_a_live_task(tmp_path: Path, monkeypatch):
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            release = asyncio.Event()

            async def _block(request, policy, **kwargs):
                await release.wait()
                raise RuntimeError("released")

            monkeypatch.setattr(app.state.pipeline, "run", _block)
            job_id = (await client.post("/v1/curate/jobs", json=_BODY)).json()["data"][
                "id"
            ]

            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry?force=true")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "already_running"

            release.set()
            await wait_for_job_status(client, job_id, {"failed"})

    await job_store.close()
    await evidence_store.close()


async def test_remote_forced_retry_sends_force(tmp_path: Path):
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        engine = CurationEngine.remote(
            "http://test", "unused", transport=httpx.ASGITransport(app=app)
        )
        try:
            job_id = await _store_orphan(job_store, JobStatus.RUNNING)
            handle = JobHandle(job_id, http_client=engine._http_client)
            requeued = await handle.retry(force=True)
            assert requeued.status is JobStatus.QUEUED
            final = await handle.wait(timeout=10)
            assert final.status is JobStatus.COMPLETED
        finally:
            await engine.close()

    await job_store.close()
    await evidence_store.close()
