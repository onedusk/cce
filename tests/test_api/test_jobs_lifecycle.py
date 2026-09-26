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
from tests.test_api.conftest import wait_for_job_status
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
    adapter=None,
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
        crawl_adapter=adapter or make_adapter(),
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
            data = await wait_for_job_status(client, job_id, {"completed", "failed"})
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
            # Wait until the background task is actually running — poll-with-
            # deadline, not a fixed sleep (T-02.01). The slow LLM then keeps
            # the job running until we delete it.
            await wait_for_job_status(client, job_id, {"running"})

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
            data = await wait_for_job_status(client, job_id, {"completed", "failed"})
            assert data["status"] == "completed"

            # Retry
            resp = await client.post(f"/v1/curate/jobs/{job_id}/retry")
            assert resp.status_code == 202

            # Poll again
            data = await wait_for_job_status(client, job_id, {"completed", "failed"})
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

            data = await wait_for_job_status(client, job_id, {"failed", "completed"})
            assert data["status"] == "failed"
            assert data["error"] is not None
            assert "crashed" in data["error"]["message"].lower()

    await job_store.close()
    await evidence_store.close()


async def test_every_cited_id_resolves_through_the_evidence_endpoint(tmp_path: Path):
    """B6 acceptance: job 1 stores an excerpt at URL A; job 2 finds the same
    text syndicated at URL B. Every evidence ID job 2's package cites must
    resolve through GET /evidence/{id} (before B6 the second copy was never
    stored and returned 404)."""
    from cce.parsing import EV_MARKER_RE
    from tests.conftest import MockCrawlAdapter, make_crawl_result

    page = (
        "# Wire story\n\nThis syndicated paragraph was published word for word "
        "by two different outlets, so both URLs carry the same excerpt text."
    )
    adapter = MockCrawlAdapter(
        search_map={
            "topic a": ["https://a.example/s"],
            "topic b": ["https://b.example/s"],
        },
        url_map={
            u: make_crawl_result(url=u, markdown=page)
            for u in ("https://a.example/s", "https://b.example/s")
        },
    )
    app, job_store, evidence_store = await _make_lifecycle_app(
        tmp_path, llm_responses=[writer_json(), verifier_json()] * 2, adapter=adapter
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for topic in ("topic a", "topic b"):
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={
                        "topic": topic,
                        "paths": ["blog"],
                        "policy_id": "test-policy",
                    },
                )
                job_id = resp.json()["data"]["id"]
                data = await wait_for_job_status(
                    client, job_id, {"completed", "failed"}
                )
                assert data["status"] == "completed"

            pkg = (await client.get(f"/v1/curate/jobs/{job_id}/package")).json()["data"]
            ids = {ev["id"] for ev in pkg["evidence"]}
            for unit in pkg["units"]:
                ids.update(c["evidence_id"] for c in unit["citations"])
                ids.update(
                    m.group(1) or m.group(2)
                    for m in EV_MARKER_RE.finditer(unit["content"])
                )
            assert ids
            for ev_id in ids:
                resp = await client.get(f"/v1/curate/evidence/{ev_id}")
                assert resp.status_code == 200, ev_id
                assert resp.json()["data"]["url"] == "https://b.example/s"

    await job_store.close()
    await evidence_store.close()


async def test_review_package_verification_over_the_api(tmp_path: Path):
    """B7 round trip, API: GET /jobs/{id}/package carries the verification
    records of a REVIEW job."""
    from tests.test_orchestrator.test_pipeline_verification_records import (
        uncited_report,
        writer_reply,
    )

    app, job_store, evidence_store = await _make_lifecycle_app(
        tmp_path, llm_responses=[writer_reply(), uncited_report()] * 3
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
            data = await wait_for_job_status(
                client, job_id, {"completed", "failed", "review_required"}
            )
            assert data["status"] == "review_required"

            pkg = (await client.get(f"/v1/curate/jobs/{job_id}/package")).json()["data"]
            [record] = pkg["verification"]
            assert record["decision"] == "review"
            assert "Max iterations" in record["feedback"]
            assessments = [c["assessment"] for c in record["report"]["claims"]]
            assert assessments.count("uncited") == 2

    await job_store.close()
    await evidence_store.close()
