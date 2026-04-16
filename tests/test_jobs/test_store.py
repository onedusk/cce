"""Tests for cce.jobs.store — JobStore SQLite CRUD operations."""

from __future__ import annotations

import pytest

from cce.jobs.store import JOB_SCHEMA_VERSION, JobStore
from cce.models.job import JobStatus
from tests.conftest import make_job, make_publish_package

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_schema_creates_tables(job_store: JobStore):
    """connect() should create jobs, packages, api_keys tables + indexes."""
    db = job_store._db
    assert db is not None
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cursor:
        tables = {row[0] for row in await cursor.fetchall()}
    assert "jobs" in tables
    assert "packages" in tables
    assert "api_keys" in tables
    assert "_meta" in tables


async def test_schema_version_stored(job_store: JobStore):
    """Schema version should be stored under namespaced key."""
    db = job_store._db
    assert db is not None
    async with db.execute(
        "SELECT value FROM _meta WHERE key = 'jobs_schema_version'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == str(JOB_SCHEMA_VERSION)


async def test_schema_indexes_created(job_store: JobStore):
    db = job_store._db
    assert db is not None
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ) as cursor:
        indexes = {row[0] for row in await cursor.fetchall()}
    assert "idx_jobs_status" in indexes
    assert "idx_jobs_created" in indexes
    assert "idx_jobs_topic" in indexes


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


async def test_create_and_get_job(job_store: JobStore):
    """create_job + get_job round-trip preserves all fields."""
    job = make_job(id="job_roundtrip")
    await job_store.create_job(job)

    loaded = await job_store.get_job("job_roundtrip")
    assert loaded is not None
    assert loaded.id == "job_roundtrip"
    assert loaded.status == JobStatus.QUEUED
    assert loaded.request.topic == job.request.topic
    assert loaded.request.policy_id == job.request.policy_id


async def test_get_nonexistent_job(job_store: JobStore):
    assert await job_store.get_job("nonexistent") is None


async def test_update_job(job_store: JobStore):
    job = make_job(id="job_update")
    await job_store.create_job(job)

    job.status = JobStatus.RUNNING
    await job_store.update_job(job)

    loaded = await job_store.get_job("job_update")
    assert loaded is not None
    assert loaded.status == JobStatus.RUNNING


async def test_list_jobs_all(job_store: JobStore):
    for i in range(3):
        await job_store.create_job(make_job(id=f"job_list_{i}"))

    jobs = await job_store.list_jobs()
    assert len(jobs) == 3


async def test_list_jobs_filter_by_status(job_store: JobStore):
    j1 = make_job(id="job_queued")
    j2 = make_job(id="job_completed")
    j2.status = JobStatus.COMPLETED
    await job_store.create_job(j1)
    await job_store.create_job(j2)

    queued = await job_store.list_jobs(status=JobStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].id == "job_queued"


async def test_list_jobs_filter_by_topic(job_store: JobStore):
    from tests.conftest import make_curation_request

    j1 = make_job(id="job_t1", request=make_curation_request(topic="alpha"))
    j2 = make_job(id="job_t2", request=make_curation_request(topic="beta"))
    await job_store.create_job(j1)
    await job_store.create_job(j2)

    result = await job_store.list_jobs(topic="alpha")
    assert len(result) == 1
    assert result[0].id == "job_t1"


async def test_list_jobs_pagination(job_store: JobStore):
    for i in range(5):
        await job_store.create_job(make_job(id=f"job_page_{i}"))

    page1 = await job_store.list_jobs(limit=2, offset=0)
    page2 = await job_store.list_jobs(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].id != page2[0].id


async def test_delete_job(job_store: JobStore):
    await job_store.create_job(make_job(id="job_del"))
    assert await job_store.delete_job("job_del") is True
    assert await job_store.get_job("job_del") is None


async def test_delete_job_nonexistent(job_store: JobStore):
    assert await job_store.delete_job("nope") is False


async def test_delete_job_cascades_to_package(job_store: JobStore):
    """delete_job should remove associated package too."""
    await job_store.create_job(make_job(id="job_cascade"))
    await job_store.store_package("job_cascade", make_publish_package(job_id="job_cascade"))

    assert await job_store.get_package("job_cascade") is not None
    await job_store.delete_job("job_cascade")
    assert await job_store.get_package("job_cascade") is None


# ---------------------------------------------------------------------------
# Package CRUD
# ---------------------------------------------------------------------------


async def test_store_and_get_package(job_store: JobStore):
    pkg = make_publish_package(job_id="job_pkg")
    await job_store.create_job(make_job(id="job_pkg"))
    await job_store.store_package("job_pkg", pkg)

    loaded = await job_store.get_package("job_pkg")
    assert loaded is not None
    assert loaded.job_id == "job_pkg"
    assert len(loaded.units) == 1
    assert loaded.scores.confidence == 0.9


async def test_get_package_nonexistent(job_store: JobStore):
    assert await job_store.get_package("nope") is None


# ---------------------------------------------------------------------------
# API Key CRUD
# ---------------------------------------------------------------------------


async def test_store_and_verify_api_key(job_store: JobStore):
    await job_store.store_api_key("abc123hash", label="test key")
    assert await job_store.verify_api_key("abc123hash") is True


async def test_verify_nonexistent_key(job_store: JobStore):
    assert await job_store.verify_api_key("nope") is False


async def test_list_api_keys(job_store: JobStore):
    await job_store.store_api_key("hash1", label="key one")
    await job_store.store_api_key("hash2", label="key two")

    keys = await job_store.list_api_keys()
    assert len(keys) == 2
    labels = {k["label"] for k in keys}
    assert labels == {"key one", "key two"}


async def test_delete_api_key(job_store: JobStore):
    await job_store.store_api_key("hash_del", label="doomed")
    assert await job_store.delete_api_key("hash_del") is True
    assert await job_store.verify_api_key("hash_del") is False


async def test_delete_api_key_nonexistent(job_store: JobStore):
    assert await job_store.delete_api_key("nope") is False
