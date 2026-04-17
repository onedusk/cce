"""Lifecycle integration tests — POST → pipeline runs → GET returns results."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cce.api.app import create_app
from cce.config.types import APIConfig, EvidenceStoreConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    llm as make_llm,
)
from tests.test_orchestrator.conftest import (
    make_adapter,
    verifier_json,
    writer_json,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_lifecycle_app(
    tmp_path: Path,
    *,
    llm_responses: list[str] | None = None,
    require_auth: bool = False,
) -> tuple[FastAPI, JobStore, SQLiteEvidenceStore]:
    """Build a fully wired app with a real Pipeline using mock deps."""
    config = make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "lifecycle.db"),
        api=APIConfig(require_auth=require_auth),
    )

    job_store = JobStore(db_path=tmp_path / "lifecycle_jobs.db")
    await job_store.connect()

    evidence_store = SQLiteEvidenceStore(config.evidence_store)
    await evidence_store.connect()

    if llm_responses is None:
        # Default: one writer + one verifier response per path (1 path = "blog")
        llm_responses = [writer_json(), verifier_json()]

    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=evidence_store,
        llm=make_llm(*llm_responses),
    )

    policies = {"test-policy": make_source_policy()}

    app = create_app(
        config=config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=pipeline,
        policies=policies,
    )
    return app, job_store, evidence_store


async def _poll_until(
    client: httpx.AsyncClient,
    job_id: str,
    terminal_statuses: set[str],
    timeout: float = 5.0,
) -> dict:
    """Poll GET /jobs/{id} until status is terminal or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/v1/curate/jobs/{job_id}")
        data = resp.json()["data"]
        if data["status"] in terminal_statuses:
            return data
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Job {job_id} did not reach {terminal_statuses} within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


async def test_full_lifecycle_post_to_package(tmp_path: Path):
    """POST → pipeline runs → COMPLETED → GET package returns content."""
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Submit
            resp = await client.post(
                "/v1/curate/jobs",
                json={
                    "topic": "test topic",
                    "paths": ["blog"],
                    "policy_id": "test-policy",
                },
            )
            assert resp.status_code == 202
            job_id = resp.json()["data"]["id"]

            # Poll until complete
            data = await _poll_until(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"

            # Get package
            resp = await client.get(f"/v1/curate/jobs/{job_id}/package")
            assert resp.status_code == 200
            pkg = resp.json()["data"]
            assert pkg["job_id"] == job_id
            assert len(pkg["units"]) == 1

    await job_store.close()
    await evidence_store.close()


async def test_delete_running_job(tmp_path: Path):
    """POST → DELETE while running → job removed."""
    # Use a slow LLM to ensure job is still running when we delete
    from cce.llm.base import LLMResponse

    class SlowMockLLM:
        async def complete(self, messages, **kwargs):
            await asyncio.sleep(10)  # Never completes in time
            return LLMResponse(content="{}", model="mock", stop_reason="end_turn")

    config = make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "slow.db"),
        api=APIConfig(require_auth=False),
    )
    job_store = JobStore(db_path=tmp_path / "slow_jobs.db")
    await job_store.connect()
    evidence_store = SQLiteEvidenceStore(config.evidence_store)
    await evidence_store.connect()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=evidence_store,
        llm=SlowMockLLM(),
    )

    app = create_app(
        config=config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=pipeline,
        policies={"test-policy": make_source_policy()},
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/curate/jobs",
                json={
                    "topic": "test topic",
                    "paths": ["blog"],
                    "policy_id": "test-policy",
                },
            )
            job_id = resp.json()["data"]["id"]
            await asyncio.sleep(0.05)

            # Delete while running
            resp = await client.delete(f"/v1/curate/jobs/{job_id}")
            assert resp.status_code == 200

            # Verify gone
            resp = await client.get(f"/v1/curate/jobs/{job_id}")
            assert resp.status_code == 404

    await job_store.close()
    await evidence_store.close()


async def test_retry_completed_job(tmp_path: Path):
    """POST → COMPLETED → retry → COMPLETED again."""
    # Need double the LLM responses for two pipeline runs
    responses = [writer_json(), verifier_json(), writer_json(), verifier_json()]
    app, job_store, evidence_store = await _make_lifecycle_app(
        tmp_path, llm_responses=responses
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First run
            resp = await client.post(
                "/v1/curate/jobs",
                json={
                    "topic": "test topic",
                    "paths": ["blog"],
                    "policy_id": "test-policy",
                },
            )
            job_id = resp.json()["data"]["id"]
            data = await _poll_until(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"

            # Retry
            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry")
            assert resp.status_code == 202

            # Poll again
            data = await _poll_until(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"

    await job_store.close()
    await evidence_store.close()


async def test_auth_required_rejects_unauthenticated(tmp_path: Path):
    """With auth enabled, POST without token returns 401."""
    app, job_store, evidence_store = await _make_lifecycle_app(
        tmp_path, require_auth=True
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/curate/jobs",
                json={"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
            )
            assert resp.status_code == 401

    await job_store.close()
    await evidence_store.close()


async def test_pipeline_error_sets_failed_status(tmp_path: Path):
    """When pipeline raises, job status becomes FAILED with error details."""

    class FailingLLM:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("LLM provider crashed")

    config = make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "fail.db"),
        api=APIConfig(require_auth=False),
    )
    job_store = JobStore(db_path=tmp_path / "fail_jobs.db")
    await job_store.connect()
    evidence_store = SQLiteEvidenceStore(config.evidence_store)
    await evidence_store.connect()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=evidence_store,
        llm=FailingLLM(),
    )

    app = create_app(
        config=config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=pipeline,
        policies={"test-policy": make_source_policy()},
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/curate/jobs",
                json={
                    "topic": "test topic",
                    "paths": ["blog"],
                    "policy_id": "test-policy",
                },
            )
            job_id = resp.json()["data"]["id"]

            data = await _poll_until(client, job_id, {"failed", "completed"})
            assert data["status"] == "failed"
            assert data["error"] is not None
            assert "crashed" in data["error"]["message"].lower()

    await job_store.close()
    await evidence_store.close()
