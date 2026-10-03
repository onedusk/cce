"""JobStore.update_job moves the stored updated_at (OPS-06)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cce.jobs.store import JobStore
from cce.models.job import JobStatus
from tests.conftest import make_job

pytestmark = pytest.mark.unit

_LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def test_update_job_bumps_updated_at(job_store: JobStore):
    job = make_job(created_at=_LONG_AGO, updated_at=_LONG_AGO)
    await job_store.create_job(job)

    job.status = JobStatus.RUNNING
    await job_store.update_job(job)

    stored = await job_store.get_job(job.id)
    assert stored is not None
    assert stored.updated_at > _LONG_AGO
    assert stored.updated_at == job.updated_at
    assert stored.created_at == _LONG_AGO
