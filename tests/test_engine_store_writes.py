"""run_pipeline_task's write-back: package before status (COR-14), a bounded
retry and the pipeline's own outcome kept when a write fails (OPS-05)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cce.engine import run_pipeline_task
from cce.jobs.store import JobStore
from cce.models.job import Job, JobError, JobStage, JobStatus, StageRecord
from cce.orchestrator.pipeline import PipelineResult
from tests.conftest import make_job, make_publish_package, make_source_policy

pytestmark = pytest.mark.integration


class _Finished:
    """A pipeline whose run finishes with the given outcome and a package."""

    def __init__(
        self,
        status: JobStatus = JobStatus.COMPLETED,
        error: JobError | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._status = status
        self._error = error
        self._exc = exc

    async def run(self, request, policy, *, job_id):
        now = datetime.now(UTC)
        job = Job(
            id=job_id,
            request=request,
            status=self._status,
            error=self._error,
            stages=[
                StageRecord(stage=JobStage.DISCOVER, started_at=now, completed_at=now)
            ],
        )
        return PipelineResult(make_publish_package(), job, [], error=self._exc)


async def _run(store: JobStore, pipeline, **kwargs) -> str:
    job = make_job()
    await store.create_job(job)
    await run_pipeline_task(
        job.id,
        job.request,
        make_source_policy(),
        pipeline=pipeline,
        job_store=store,
        semaphore=asyncio.Semaphore(1),
        running_tasks={},
        **kwargs,
    )
    return job.id


def _fail_update_job(
    store: JobStore, monkeypatch, *, status: JobStatus, times: int
) -> list[JobStatus]:
    """Make the first ``times`` writes of ``status`` raise 'database is locked'.

    Returns the list every attempted status is appended to.
    """
    attempted: list[JobStatus] = []
    real = store.update_job

    async def _update_job(job):
        attempted.append(job.status)
        if job.status is status and attempted.count(status) <= times:
            raise sqlite3.OperationalError("database is locked")
        await real(job)

    monkeypatch.setattr(store, "update_job", _update_job)
    return attempted


async def test_package_is_stored_before_the_terminal_status(
    job_store: JobStore, monkeypatch
):
    """COR-14: no reader sees a terminal job whose package is still to come."""
    seen: list[JobStatus] = []
    real = job_store.store_package

    async def _store_package(job_id, package):
        stored = await job_store.get_job(job_id)
        assert stored is not None
        seen.append(stored.status)
        await real(job_id, package)

    monkeypatch.setattr(job_store, "store_package", _store_package)

    job_id = await _run(job_store, _Finished())

    assert seen == [JobStatus.RUNNING]
    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    package = await job_store.get_package(job_id)
    assert package is not None
    assert package.job_id == job_id


async def test_failed_package_write_never_shows_the_job_completed(
    job_store: JobStore, monkeypatch
):
    """COR-14: a package that cannot be stored fails the job; it is never
    COMPLETED first and never COMPLETED without its package. The failure is
    recorded at 'publish' and the run's stage records are kept."""
    written: list[JobStatus] = []
    real = job_store.update_job

    async def _update_job(job):
        written.append(job.status)
        await real(job)

    async def _store_package(job_id, package):
        raise OSError("disk full")

    monkeypatch.setattr(job_store, "update_job", _update_job)
    monkeypatch.setattr(job_store, "store_package", _store_package)
    errors: list[BaseException] = []

    job_id = await _run(job_store, _Finished(), on_error=errors.append)

    assert written == [JobStatus.RUNNING, JobStatus.FAILED]
    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.error is not None
    assert stored.error == JobError(
        code="pipeline_error", message="disk full", stage=JobStage.PUBLISH
    )
    assert [r.stage for r in stored.stages] == [JobStage.DISCOVER]
    assert await job_store.get_package(job_id) is None
    assert [str(e) for e in errors] == ["disk full"]


async def test_failed_run_keeps_its_own_error_when_its_package_cannot_be_stored(
    job_store: JobStore, monkeypatch
):
    """OPS-05: a FAILED run's record is true without its (partial) package,
    so a store error does not replace the run's own error."""
    own_error = JobError(
        code="unparseable_response", message="writer reply", stage=JobStage.WRITE
    )
    own_exc = ValueError("writer reply")

    async def _store_package(job_id, package):
        raise OSError("disk full")

    monkeypatch.setattr(job_store, "store_package", _store_package)
    errors: list[BaseException] = []

    job_id = await _run(
        job_store,
        _Finished(JobStatus.FAILED, own_error, own_exc),
        on_error=errors.append,
    )

    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.error == own_error
    assert await job_store.get_package(job_id) is None
    assert errors == [own_exc]


async def test_one_locked_terminal_write_is_retried(job_store: JobStore, monkeypatch):
    """OPS-05: one 'database is locked' no longer fails a finished run."""
    monkeypatch.setattr("cce.engine._STORE_WRITE_RETRY_DELAY_S", 0)
    attempted = _fail_update_job(
        job_store, monkeypatch, status=JobStatus.COMPLETED, times=1
    )

    job_id = await _run(job_store, _Finished())

    assert attempted == [JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.COMPLETED]
    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert stored.error is None
    assert await job_store.get_package(job_id) is not None


async def test_exhausted_terminal_write_keeps_the_pipelines_own_outcome(
    job_store: JobStore, monkeypatch
):
    """OPS-05: when every retry of the status write fails, the handler stores
    the run's own status and error, not pipeline_error at 'discover'."""
    monkeypatch.setattr("cce.engine._STORE_WRITE_RETRY_DELAY_S", 0)
    own_error = JobError(
        code="unparseable_response", message="writer reply", stage=JobStage.WRITE
    )
    own_exc = ValueError("writer reply")
    attempted = _fail_update_job(
        job_store, monkeypatch, status=JobStatus.FAILED, times=3
    )
    errors: list[BaseException] = []

    job_id = await _run(
        job_store,
        _Finished(JobStatus.FAILED, own_error, own_exc),
        on_error=errors.append,
    )

    assert attempted == [JobStatus.RUNNING] + [JobStatus.FAILED] * 4
    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.error == own_error
    assert stored.completed_at is not None
    assert await job_store.get_package(job_id) is not None
    # The handle keeps the run's own exception, not the store's.
    assert errors == [own_exc]


async def test_write_back_survives_a_lock_held_past_the_busy_timeout(
    tmp_path: Path, job_store: JobStore, monkeypatch
):
    """OPS-05 against real SQLite: another connection holds the write lock
    past the busy timeout, a status poll lands on the store's connection, the
    holder commits. The retry must land (it needs the failed write rolled
    back: the poll otherwise pins a stale snapshot that fails every write)."""
    monkeypatch.setattr("cce.engine._STORE_WRITE_RETRY_DELAY_S", 0)
    assert job_store._db is not None
    await job_store._db.execute("PRAGMA busy_timeout=50")
    holder = sqlite3.connect(tmp_path / "test_jobs.db", isolation_level=None)

    class _FinishedUnderLock(_Finished):
        async def run(self, request, policy, *, job_id):
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE jobs SET topic = topic")
            return await super().run(request, policy, job_id=job_id)

    real = job_store.store_package
    failures: list[str] = []

    async def _store_package(job_id, package):
        try:
            await real(job_id, package)
        except sqlite3.OperationalError as e:
            failures.append(str(e))
            await job_store.get_job(job_id)  # a poll, as JobHandle.wait() does
            holder.execute("COMMIT")
            raise

    monkeypatch.setattr(job_store, "store_package", _store_package)
    try:
        job_id = await _run(job_store, _FinishedUnderLock())
    finally:
        holder.close()

    assert failures == ["database is locked"]
    stored = await job_store.get_job(job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert stored.error is None
    assert await job_store.get_package(job_id) is not None
