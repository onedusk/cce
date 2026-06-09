"""Tests for CurationEngine — embedded + remote modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cce.engine import CurationEngine, JobHandle
from cce.models.job import JobStatus
from cce.models.request import CurationRequest
from cce.orchestrator.pipeline import Pipeline
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


async def _make_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_responses: list[str] | None = None,
) -> CurationEngine:
    """Build a CurationEngine through the real ``embedded()`` factory.

    Real config YAML, policy YAML, env keys, and stores (T-04.03) — only the
    pipeline build is substituted (mock LLM + crawl adapter) so the pipeline
    outcome stays scripted. This is the factory-line coverage that survives
    the M05/M06 refactor (audit 1.1).
    """
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "evidence_store:\n"
        f"  sqlite_path: {tmp_path / 'engine_test.db'}\n"
        "embedding:\n"
        "  enabled: false\n"
        "api:\n"
        "  require_auth: false\n"
        "  max_concurrent_jobs: 2\n"
        "engine_version: 0.1.0-test\n"
    )
    policies_dir = tmp_path / "policies"
    policies_dir.mkdir(exist_ok=True)
    (policies_dir / "test-policy.yaml").write_text(
        "id: test-policy\nname: Test Policy\n"
    )

    # embedded() fail-fasts on missing keys (M01); dummy values satisfy it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    # Keep the YAML sqlite_path authoritative even if the host env sets one.
    monkeypatch.delenv("CCE_EVIDENCE_SQLITE_PATH", raising=False)

    if llm_responses is None:
        llm_responses = [writer_json(), verifier_json()]

    def _mock_pipeline_build(config, registry, evidence_store) -> Pipeline:
        return Pipeline(
            config=config,
            crawl_adapter=make_adapter(),
            evidence_store=evidence_store,
            llm=make_llm(*llm_responses),
        )

    monkeypatch.setattr("cce.engine.build_pipeline", _mock_pipeline_build)

    return await CurationEngine.embedded(
        config_path=str(config_yaml),
        policies_dir=str(policies_dir),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_embedded_curate_returns_job_handle(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
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


async def test_embedded_curate_wait_completes(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
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


async def test_embedded_package_after_completion(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
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


async def test_embedded_close_releases_resources(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
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


async def test_embedded_invalid_policy_raises(tmp_path: Path, monkeypatch):
    engine = await _make_engine(tmp_path, monkeypatch)
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


async def test_embedded_cancel_running_job(tmp_path: Path, monkeypatch):
    """cancel() on a RUNNING embedded job → CANCELLED; retry meanwhile refuses."""
    import asyncio

    engine = await _make_engine(tmp_path, monkeypatch)
    try:

        async def _slow(request, policy):
            await asyncio.sleep(60)

        monkeypatch.setattr(engine._pipeline, "run", _slow)

        handle = await engine.curate(
            CurationRequest(
                topic="test topic",
                paths=["blog"],
                policy_id="test-policy",
            )
        )
        # Wait until the background task has marked the job RUNNING.
        deadline = asyncio.get_running_loop().time() + 5
        while (await handle.status()).status is not JobStatus.RUNNING:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        with pytest.raises(ValueError, match="already queued or running"):
            await handle.retry()
        with pytest.raises(TimeoutError):
            await handle.wait(timeout=0)

        await handle.cancel()
        job = await handle.status()
        assert job.status is JobStatus.CANCELLED
    finally:
        await engine.close()


async def test_embedded_retry_requeues_terminal_job(tmp_path: Path, monkeypatch):
    engine = await _make_engine(
        tmp_path, monkeypatch, llm_responses=[writer_json(), verifier_json()] * 2
    )
    try:
        handle = await engine.curate(
            CurationRequest(
                topic="test topic",
                paths=["blog"],
                policy_id="test-policy",
            )
        )
        first = await handle.wait(timeout=10)
        assert first.status is JobStatus.COMPLETED

        requeued = await handle.retry()
        assert requeued.status is JobStatus.QUEUED
        assert requeued.error is None

        final = await handle.wait(timeout=10)
        assert final.status is JobStatus.COMPLETED
    finally:
        await engine.close()


async def test_pipeline_crash_marks_job_failed(tmp_path: Path, monkeypatch):
    """Pipeline exception → FAILED job with pipeline_error + task cleanup (T-04.02)."""
    engine = await _make_engine(tmp_path, monkeypatch)
    try:

        async def _boom(request, policy):
            raise RuntimeError("boom")

        monkeypatch.setattr(engine._pipeline, "run", _boom)

        handle = await engine.curate(
            CurationRequest(
                topic="test topic",
                paths=["blog"],
                policy_id="test-policy",
            )
        )
        job = await handle.wait(timeout=10)

        assert job.status is JobStatus.FAILED
        assert job.error is not None
        assert job.error.code == "pipeline_error"
        assert "boom" in job.error.message
        # The finally-block cleanup must have removed the background task.
        assert handle.job_id not in engine._running_tasks
    finally:
        await engine.close()
