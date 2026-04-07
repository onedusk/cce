"""Tests for graceful shutdown — orphan job marking."""

from __future__ import annotations

from pathlib import Path

import pytest

from cce.jobs.store import JobStore
from cce.models.job import JobError, JobStage, JobStatus
from tests.conftest import make_job


async def _mark_orphaned_jobs(job_store: JobStore) -> int:
    """Replicate the orphan-marking logic from app.py shutdown handler."""
    running_jobs = await job_store.list_jobs(status=JobStatus.RUNNING)
    for job in running_jobs:
        job.status = JobStatus.FAILED
        job.error = JobError(
            code="server_shutdown",
            message="Server shut down while job was running",
            stage=job.stage or JobStage.DISCOVER,
        )
        await job_store.update_job(job)
    return len(running_jobs)


class TestGracefulShutdown:
    async def test_no_orphans_when_no_running_jobs(self, tmp_path):
        store = JobStore(db_path=tmp_path / "test.db")
        await store.connect()
        try:
            # Create a COMPLETED job — should not be touched
            completed_job = make_job(status=JobStatus.COMPLETED)
            await store.create_job(completed_job)

            count = await _mark_orphaned_jobs(store)

            assert count == 0
            # Verify the completed job is unchanged
            fetched = await store.get_job(completed_job.id)
            assert fetched is not None
            assert fetched.status == JobStatus.COMPLETED
        finally:
            await store.close()

    async def test_orphan_running_job_marked_failed(self, tmp_path):
        store = JobStore(db_path=tmp_path / "test.db")
        await store.connect()
        try:
            # Create a RUNNING job — simulates a job that was in progress
            running_job = make_job(status=JobStatus.RUNNING)
            running_job.stage = JobStage.WRITE
            await store.create_job(running_job)

            count = await _mark_orphaned_jobs(store)

            assert count == 1
            fetched = await store.get_job(running_job.id)
            assert fetched is not None
            assert fetched.status == JobStatus.FAILED
            assert fetched.error is not None
            assert fetched.error.code == "server_shutdown"
            assert fetched.error.stage == JobStage.WRITE
        finally:
            await store.close()

    async def test_multiple_orphans_all_marked(self, tmp_path):
        store = JobStore(db_path=tmp_path / "test.db")
        await store.connect()
        try:
            # Create 3 RUNNING jobs
            jobs = []
            for stage in [JobStage.DISCOVER, JobStage.WRITE, JobStage.VERIFY]:
                job = make_job(status=JobStatus.RUNNING)
                job.stage = stage
                await store.create_job(job)
                jobs.append(job)

            count = await _mark_orphaned_jobs(store)

            assert count == 3
            for job in jobs:
                fetched = await store.get_job(job.id)
                assert fetched is not None
                assert fetched.status == JobStatus.FAILED
                assert fetched.error.code == "server_shutdown"
        finally:
            await store.close()

    async def test_orphan_with_no_stage_uses_fallback(self, tmp_path):
        store = JobStore(db_path=tmp_path / "test.db")
        await store.connect()
        try:
            # RUNNING job with stage=None (e.g., failed during early handoff)
            job = make_job(status=JobStatus.RUNNING)
            job.stage = None
            await store.create_job(job)

            count = await _mark_orphaned_jobs(store)

            assert count == 1
            fetched = await store.get_job(job.id)
            assert fetched is not None
            assert fetched.status == JobStatus.FAILED
            assert fetched.error.stage == JobStage.DISCOVER  # fallback
        finally:
            await store.close()
