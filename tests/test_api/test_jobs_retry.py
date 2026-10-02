"""POST /v1/curate/jobs/{id}/retry lifecycle (COR-02)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

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
            job_id = (await client.post("/v1/curate/jobs", json=_BODY)).json()[
                "data"
            ]["id"]
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
            job_id = (await client.post("/v1/curate/jobs", json=_BODY)).json()[
                "data"
            ]["id"]
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
