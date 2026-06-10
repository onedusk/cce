"""Tests for graceful shutdown — exercises the REAL lifespan handler.

Each test builds the app with injected components, enters the lifespan
context itself, mutates state inside it, then exits and asserts on the
post-shutdown state. No replicated production logic (T-04.04, finding 3.2).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

import cce.api.app as app_module
from cce.api.app import create_app
from cce.jobs.store import JobStore
from cce.models.job import JobStage, JobStatus
from tests.conftest import make_job

pytestmark = pytest.mark.integration


@pytest.fixture
def shutdown_app(
    test_config,
    job_store: JobStore,
    evidence_store,
    mock_pipeline,
    test_policies,
) -> FastAPI:
    """App wired with injected components; lifespan NOT entered here — each
    test drives the context itself so it can assert after shutdown."""
    return create_app(
        config=test_config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=mock_pipeline,
        policies=test_policies,
    )


class TestGracefulShutdown:
    async def test_no_orphans_when_no_running_jobs(
        self, shutdown_app: FastAPI, job_store: JobStore
    ):
        completed_job = make_job(status=JobStatus.COMPLETED)
        async with shutdown_app.router.lifespan_context(shutdown_app):
            await job_store.create_job(completed_job)

        # Shutdown must not touch non-RUNNING jobs.
        fetched = await job_store.get_job(completed_job.id)
        assert fetched is not None
        assert fetched.status == JobStatus.COMPLETED
        assert fetched.error is None

    async def test_orphan_running_job_marked_failed(
        self, shutdown_app: FastAPI, job_store: JobStore
    ):
        running_job = make_job(status=JobStatus.RUNNING)
        running_job.stage = JobStage.WRITE
        async with shutdown_app.router.lifespan_context(shutdown_app):
            await job_store.create_job(running_job)

        fetched = await job_store.get_job(running_job.id)
        assert fetched is not None
        assert fetched.status == JobStatus.FAILED
        assert fetched.error is not None
        assert fetched.error.code == "server_shutdown"
        assert fetched.error.stage == JobStage.WRITE

    async def test_multiple_orphans_all_marked(
        self, shutdown_app: FastAPI, job_store: JobStore
    ):
        jobs = []
        async with shutdown_app.router.lifespan_context(shutdown_app):
            for stage in [JobStage.DISCOVER, JobStage.WRITE, JobStage.VERIFY]:
                job = make_job(status=JobStatus.RUNNING)
                job.stage = stage
                await job_store.create_job(job)
                jobs.append(job)

        for job in jobs:
            fetched = await job_store.get_job(job.id)
            assert fetched is not None
            assert fetched.status == JobStatus.FAILED
            assert fetched.error is not None
            assert fetched.error.code == "server_shutdown"

    async def test_orphan_with_no_stage_uses_fallback(
        self, shutdown_app: FastAPI, job_store: JobStore
    ):
        job = make_job(status=JobStatus.RUNNING)
        job.stage = None
        async with shutdown_app.router.lifespan_context(shutdown_app):
            await job_store.create_job(job)

        fetched = await job_store.get_job(job.id)
        assert fetched is not None
        assert fetched.status == JobStatus.FAILED
        assert fetched.error is not None
        assert fetched.error.stage == JobStage.DISCOVER  # fallback

    async def test_shutdown_waits_for_tasks_that_finish_in_time(
        self, shutdown_app: FastAPI
    ):
        """Tasks finishing inside the grace window drain — not cancelled."""
        finished = asyncio.Event()

        async def _quick() -> None:
            await asyncio.sleep(0.05)
            finished.set()

        async with shutdown_app.router.lifespan_context(shutdown_app):
            task = asyncio.create_task(_quick())
            shutdown_app.state.running_tasks["job_quick"] = task

        assert finished.is_set()
        assert not task.cancelled()

    async def test_shutdown_cancels_tasks_exceeding_timeout(
        self, shutdown_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ):
        """Stragglers past SHUTDOWN_TIMEOUT_S get drain-then-cancel treatment."""
        monkeypatch.setattr(app_module, "SHUTDOWN_TIMEOUT_S", 0.1)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _stuck() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async with shutdown_app.router.lifespan_context(shutdown_app):
            task = asyncio.create_task(_stuck())
            shutdown_app.state.running_tasks["job_stuck"] = task
            await started.wait()

        assert cancelled.is_set()
        assert task.cancelled()
