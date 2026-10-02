"""Embedded-mode retry lifecycle: a clean slate (COR-02), orphans (OPS-04)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cce.engine import JobHandle
from cce.models.job import Job, JobStatus
from cce.models.request import CurationRequest
from tests.test_engine import _make_engine

pytestmark = pytest.mark.integration


def _request() -> CurationRequest:
    return CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")


async def test_failed_retry_does_not_serve_the_previous_package(
    tmp_path: Path, monkeypatch
):
    """COR-02: while a retry runs and after it fails, the job has no package
    and none of the previous run's stage records."""
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await engine.curate(_request())
        first = await handle.wait(timeout=10)
        assert first.status is JobStatus.COMPLETED
        assert first.stages
        assert await handle.package() is not None

        release = asyncio.Event()

        async def _fail_later(request, policy, **kwargs):
            await release.wait()
            raise RuntimeError("retry failed")

        monkeypatch.setattr(engine._pipeline, "run", _fail_later)

        requeued = await handle.retry()
        assert requeued.stages == []
        assert await handle.package() is None
        assert (await handle.status()).stages == []

        release.set()
        final = await handle.wait(timeout=10)
        assert final.status is JobStatus.FAILED
        assert await handle.package() is None
    finally:
        await engine.close()


async def test_retry_with_unknown_policy_leaves_job_untouched(
    tmp_path: Path, monkeypatch
):
    """A retry whose policy is gone raises before any write: the job keeps its
    status and package instead of being left QUEUED with no task."""
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await engine.curate(_request())
        assert (await handle.wait(timeout=10)).status is JobStatus.COMPLETED

        monkeypatch.setattr(engine, "_policies", {})
        with pytest.raises(ValueError, match="Policy not found"):
            await handle.retry()

        assert (await handle.status()).status is JobStatus.COMPLETED
        assert await handle.package() is not None
    finally:
        await engine.close()


# ---------------------------------------------------------------------------
# OPS-04: a job a crash left QUEUED or RUNNING
# ---------------------------------------------------------------------------


async def _orphan(engine, status: JobStatus) -> JobHandle:
    """Store a job in ``status`` with no task behind it, as after a crash."""
    assert engine._job_store is not None
    job = Job(id="job_orphan000001", request=_request(), status=status)
    await engine._job_store.create_job(job)
    return JobHandle(
        job.id,
        job_store=engine._job_store,
        running_tasks=engine._running_tasks,
        engine=engine,
    )


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.QUEUED])
async def test_forced_retry_recovers_an_orphaned_job(
    tmp_path: Path, monkeypatch, status: JobStatus
):
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await _orphan(engine, status)
        with pytest.raises(ValueError, match="already queued or running"):
            await handle.retry()

        assert engine._job_store is not None
        written: list[tuple[JobStatus, str | None]] = []
        update_job = engine._job_store.update_job

        async def _record(job):
            written.append((job.status, job.error.code if job.error else None))
            await update_job(job)

        monkeypatch.setattr(engine._job_store, "update_job", _record)

        requeued = await handle.retry(force=True)
        assert requeued.status is JobStatus.QUEUED
        assert requeued.error is None
        assert written[:2] == [
            (JobStatus.FAILED, "orphaned"),
            (JobStatus.QUEUED, None),
        ]
        final = await handle.wait(timeout=10)
        assert final.status is JobStatus.COMPLETED
    finally:
        await engine.close()


async def test_forced_retry_refuses_a_job_with_a_live_task(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
    try:

        async def _slow(request, policy, **kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(engine._pipeline, "run", _slow)
        handle = await engine.curate(_request())
        with pytest.raises(ValueError, match="already queued or running"):
            await handle.retry(force=True)
        assert (await handle.status()).status in (JobStatus.QUEUED, JobStatus.RUNNING)
    finally:
        await engine.close()


async def test_cancel_marks_an_orphaned_job_failed(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await _orphan(engine, JobStatus.RUNNING)
        await handle.cancel()
        job = await handle.status()
        assert job.status is JobStatus.FAILED
        assert job.error is not None
        assert job.error.code == "orphaned"
        assert job.completed_at is not None
    finally:
        await engine.close()
