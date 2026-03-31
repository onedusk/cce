"""Tests for CurationEngine — embedded + remote modes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cce.config.types import APIConfig, EvidenceStoreConfig
from cce.engine import CurationEngine, JobHandle
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.models.job import JobStatus
from cce.models.request import CurationRequest
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockCrawlAdapter,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    llm as make_llm,
    make_adapter,
    verifier_json,
    writer_json,
)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine(tmp_path: Path, llm_responses: list[str] | None = None) -> CurationEngine:
    """Build a CurationEngine with mock deps for testing."""
    config = make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "engine_test.db"),
        api=APIConfig(require_auth=False),
    )

    engine = CurationEngine()
    engine._mode = "embedded"
    engine._config = config

    engine._job_store = JobStore(db_path=tmp_path / "engine_jobs.db")
    await engine._job_store.connect()

    engine._evidence_store = SQLiteEvidenceStore(config.evidence_store)
    await engine._evidence_store.connect()

    if llm_responses is None:
        llm_responses = [writer_json(), verifier_json()]

    engine._pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=engine._evidence_store,
        llm=make_llm(*llm_responses),
    )

    engine._policies = {"test-policy": make_source_policy()}
    engine._semaphore = asyncio.Semaphore(2)

    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_embedded_curate_returns_job_handle(tmp_path: Path):
    engine = await _make_engine(tmp_path)
    try:
        request = CurationRequest(
            topic="test topic",
            paths=["blog"],
            policy_id="test-policy",
        )
        handle = await engine.curate(request)
        assert isinstance(handle, JobHandle)
        assert handle.job_id.startswith("job_")
    finally:
        await engine.close()


async def test_embedded_curate_wait_completes(tmp_path: Path):
    engine = await _make_engine(tmp_path)
    try:
        request = CurationRequest(
            topic="test topic",
            paths=["blog"],
            policy_id="test-policy",
        )
        handle = await engine.curate(request)
        job = await handle.wait(timeout=10)
        assert job.status == JobStatus.COMPLETED
    finally:
        await engine.close()


async def test_embedded_package_after_completion(tmp_path: Path):
    engine = await _make_engine(tmp_path)
    try:
        request = CurationRequest(
            topic="test topic",
            paths=["blog"],
            policy_id="test-policy",
        )
        handle = await engine.curate(request)
        await handle.wait(timeout=10)

        package = await handle.package()
        assert package is not None
        assert package.job_id == handle.job_id
        assert len(package.units) == 1
    finally:
        await engine.close()


async def test_embedded_close_releases_resources(tmp_path: Path):
    engine = await _make_engine(tmp_path)
    assert engine._job_store is not None
    assert engine._evidence_store is not None

    await engine.close()
    assert engine._job_store._db is None
    assert engine._evidence_store._db is None


async def test_remote_instantiation():
    engine = CurationEngine.remote("http://localhost:8000", "test-key")
    assert engine._mode == "remote"
    assert engine._http_client is not None
    assert "Bearer test-key" in engine._http_client.headers["Authorization"]
    await engine.close()


async def test_embedded_invalid_policy_raises(tmp_path: Path):
    engine = await _make_engine(tmp_path)
    try:
        request = CurationRequest(
            topic="test",
            paths=["blog"],
            policy_id="nonexistent",
        )
        with pytest.raises(ValueError, match="Policy not found"):
            await engine.curate(request)
    finally:
        await engine.close()
