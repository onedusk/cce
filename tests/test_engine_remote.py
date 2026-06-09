"""Remote-mode integration tests for CurationEngine via httpx.ASGITransport.

Drives the engine's real HTTP client against the test FastAPI app in-process
(no live server), with auth enabled so the Bearer header built by
``CurationEngine.remote()`` is what actually authenticates each request.
Covers the JobCreateRequest wire mapping and every remote JobHandle branch
(audit-2026-06-09 T-04.01, finding 3.1).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cce.api.app import create_app
from cce.api.auth import generate_api_key, hash_api_key
from cce.config.types import (
    APIConfig,
    EvidenceStoreConfig,
    default_quality_gate_profiles,
)
from cce.engine import CurationEngine, JobHandle
from cce.jobs.store import JobStore
from cce.models.job import JobStatus
from cce.models.package import PublishPackage
from cce.models.request import CurationConstraints, CurationRequest
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    make_curation_request,
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_config(tmp_path: Path):
    """EngineConfig with auth ENABLED — remote mode must authenticate."""
    return make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "remote_test.db"),
        api=APIConfig(require_auth=True),
        quality_gate=default_quality_gate_profiles(),
    )


@pytest.fixture
def remote_pipeline(remote_config, sqlite_store) -> Pipeline:
    """Real Pipeline with scripted mock LLM/crawl — enough for two full runs
    (the retry test runs the pipeline twice)."""
    responses = [writer_json(), verifier_json()] * 3
    return Pipeline(
        config=remote_config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=make_llm(*responses),
    )


@pytest.fixture
async def remote_app(
    remote_config,
    job_store: JobStore,
    sqlite_store,
    remote_pipeline: Pipeline,
) -> AsyncGenerator[FastAPI, None]:
    _app = create_app(
        config=remote_config,
        job_store=job_store,
        evidence_store=sqlite_store,
        pipeline=remote_pipeline,
        policies={"test-policy": make_source_policy()},
    )
    async with _app.router.lifespan_context(_app):
        yield _app


@pytest.fixture
async def api_key(job_store: JobStore) -> str:
    key = generate_api_key()
    await job_store.store_api_key(hash_api_key(key), label="engine-remote-test")
    return key


@pytest.fixture
async def engine(
    remote_app: FastAPI, api_key: str
) -> AsyncGenerator[CurationEngine, None]:
    eng = CurationEngine.remote(
        "http://test",
        api_key,
        transport=httpx.ASGITransport(app=remote_app),
    )
    yield eng
    await eng.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_curate_maps_request_fields_onto_wire(engine: CurationEngine):
    """curate() must map every CurationRequest field onto the wire format.

    The route persists the Job from the JobCreateRequest payload it received,
    so a field-for-field round-trip through status() verifies the mapping in
    ``CurationEngine._curate_remote``. Renaming any JobCreateRequest field
    breaks this test.
    """
    request = CurationRequest(
        topic="remote wire topic",
        subtopics=["sub-a", "sub-b"],
        paths=["blog"],
        audience="clinicians",
        constraints=CurationConstraints(jurisdiction="EU"),
        policy_id="test-policy",
        taxonomy_id="wellbeing-8d",
        path_config_id="thnklabs",
        risk_profile="high",
    )
    handle = await engine.curate(request)
    assert handle.job_id.startswith("job_")

    job = await handle.status()
    received = job.request
    assert received.topic == "remote wire topic"
    assert received.subtopics == ["sub-a", "sub-b"]
    assert received.paths == ["blog"]
    assert received.audience == "clinicians"
    assert received.policy_id == "test-policy"
    assert received.taxonomy_id == "wellbeing-8d"
    assert received.path_config_id == "thnklabs"
    assert received.risk_profile == "high"
    assert received.constraints is not None
    assert received.constraints.jurisdiction == "EU"


async def test_status_returns_job_state(engine: CurationEngine):
    handle = await engine.curate(make_curation_request())
    job = await handle.status()
    assert job.id == handle.job_id
    assert isinstance(job.status, JobStatus)


async def test_wait_polls_to_terminal_state(engine: CurationEngine):
    handle = await engine.curate(make_curation_request())
    job = await handle.wait(timeout=10)
    assert job.status is JobStatus.COMPLETED


async def test_package_returns_publish_package(engine: CurationEngine):
    handle = await engine.curate(make_curation_request())
    await handle.wait(timeout=10)

    package = await handle.package()
    assert isinstance(package, PublishPackage)
    assert package.job_id == handle.job_id
    assert len(package.units) == 1


async def test_package_returns_none_when_absent(engine: CurationEngine):
    handle = JobHandle("job_does_not_exist", http_client=engine._http_client)
    assert await handle.package() is None


async def test_cancel_deletes_job(engine: CurationEngine):
    handle = await engine.curate(make_curation_request())
    await handle.cancel()

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await handle.status()
    assert exc_info.value.response.status_code == 404
    assert await handle.package() is None


async def test_retry_requeues_terminal_job(engine: CurationEngine):
    handle = await engine.curate(make_curation_request())
    first = await handle.wait(timeout=10)
    assert first.status is JobStatus.COMPLETED

    requeued = await handle.retry()
    assert requeued.id == handle.job_id
    assert requeued.status is JobStatus.QUEUED

    final = await handle.wait(timeout=10)
    assert final.status is JobStatus.COMPLETED


async def test_remote_rejects_bad_api_key(remote_app: FastAPI):
    """The Bearer header built by remote() is load-bearing: a wrong key 401s."""
    eng = CurationEngine.remote(
        "http://test",
        "not-a-real-key",
        transport=httpx.ASGITransport(app=remote_app),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await eng.curate(make_curation_request())
        assert exc_info.value.response.status_code == 401
    finally:
        await eng.close()
