"""Tests for CurationEngine — embedded + remote modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cce.components import ComponentOverrides
from cce.engine import CurationEngine, JobHandle
from cce.models.job import JobStatus
from cce.models.request import CurationRequest
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
    extra_yaml: str = "",
) -> CurationEngine:
    """Build a CurationEngine through the real ``embedded()`` factory.

    Real config YAML, policy YAML, stores and the real ``build_pipeline`` —
    the mock LLM and crawl adapter are injected with ``ComponentOverrides``
    (B5), so no API keys are needed and the outcome stays scripted.
    Humanization is off and the taxonomy / path-config surfaces point at
    empty temp paths, so the run is hermetic (no gitignored operator files).
    """
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "evidence_store:\n"
        f"  sqlite_path: {tmp_path / 'engine_test.db'}\n"
        "embedding:\n"
        "  enabled: false\n"
        "humanization:\n"
        "  enabled: false\n"
        "api:\n"
        "  require_auth: false\n"
        "  max_concurrent_jobs: 2\n"
        "engine_version: 0.1.0-test\n" + extra_yaml
    )
    policies_dir = tmp_path / "policies"
    policies_dir.mkdir(exist_ok=True)
    (policies_dir / "test-policy.yaml").write_text(
        "id: test-policy\nname: Test Policy\n"
    )

    # Injected providers need no keys (B5): prove it by removing them.
    for var in ("ANTHROPIC_API_KEY", "CCE_LLM_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CCE_CRAWL_API_KEY", raising=False)
    # Keep the YAML sqlite_path authoritative even if the host env sets one.
    monkeypatch.delenv("CCE_EVIDENCE_SQLITE_PATH", raising=False)

    if llm_responses is None:
        llm_responses = [writer_json(), verifier_json()]

    return await CurationEngine.embedded(
        config_path=str(config_yaml),
        policies_dir=str(policies_dir),
        taxonomies_dir=str(tmp_path / "no-taxonomies"),
        path_configs_path=str(tmp_path / "no-path-configs.yaml"),
        overrides=ComponentOverrides(
            llm=make_llm(*llm_responses), crawl_adapter=make_adapter()
        ),
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


async def test_embedded_uses_injected_stores_and_leaves_them_open(
    tmp_path: Path, monkeypatch
):
    """B6: engines built with their own stores keep tenants apart (each sees
    only its own jobs), and close() leaves caller-owned stores open."""
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore
    from cce.jobs.store import JobStore

    stores = {}
    engines = {}
    for tenant in ("a", "b"):
        ev = SQLiteEvidenceStore(
            EvidenceStoreConfig(sqlite_path=tmp_path / f"{tenant}_ev.db")
        )
        jobs = JobStore(db_path=tmp_path / f"{tenant}_jobs.db")
        await ev.connect()
        await jobs.connect()
        stores[tenant] = (ev, jobs)

    # Same YAML / policies for both; only the injected stores differ.
    base = await _make_engine(tmp_path, monkeypatch)
    await base.close()
    for tenant in ("a", "b"):
        ev, jobs = stores[tenant]
        engines[tenant] = await CurationEngine.embedded(
            config_path=str(tmp_path / "config.yaml"),
            policies_dir=str(tmp_path / "policies"),
            taxonomies_dir=str(tmp_path / "no-taxonomies"),
            path_configs_path=str(tmp_path / "no-path-configs.yaml"),
            overrides=ComponentOverrides(
                llm=make_llm(writer_json(), verifier_json()),
                crawl_adapter=make_adapter(),
            ),
            evidence_store=ev,
            job_store=jobs,
        )

    try:
        handle = await engines["a"].curate(
            CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")
        )
        assert (await handle.wait(timeout=10)).status == JobStatus.COMPLETED

        a_jobs = await stores["a"][1].list_jobs()
        b_jobs = await stores["b"][1].list_jobs()
        assert [j.id for j in a_jobs] == [handle.job_id]
        assert b_jobs == []
        assert await stores["a"][0].count() > 0
        assert await stores["b"][0].count() == 0
    finally:
        for engine in engines.values():
            await engine.close()

    for ev, jobs in stores.values():
        # Still open: the caller owns them.
        assert await ev.count() >= 0
        assert await jobs.list_jobs() is not None
        await ev.close()
        await jobs.close()


async def test_embedded_review_package_carries_verification(
    tmp_path: Path, monkeypatch
):
    """B7 round trip, embedded: the stored package of a REVIEW job holds the
    per-claim verdicts and the gate's reasons."""
    from tests.test_orchestrator.test_pipeline_verification_records import (
        uncited_report,
        writer_reply,
    )

    script = [writer_reply(gaps=["gap"]), uncited_report()] * 3
    engine = await _make_engine(tmp_path, monkeypatch, llm_responses=script)
    try:
        handle = await engine.curate(
            CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")
        )
        job = await handle.wait(timeout=10)
        assert job.status == JobStatus.REVIEW_REQUIRED

        package = await handle.package()
        assert package is not None
        [record] = package.verification
        assert record.decision == "review"
        assert "no citations" in record.feedback
        assert record.writer_gaps == ["gap"]
        assert [c.assessment for c in record.report.claims].count("uncited") == 2
    finally:
        await engine.close()


async def test_embedded_wait_returns_ready_for_approval(tmp_path: Path, monkeypatch):
    """B8: READY_FOR_APPROVAL is terminal for wait() (else it would time out)."""
    engine = await _make_engine(
        tmp_path,
        monkeypatch,
        llm_responses=[writer_json(), verifier_json(supported=10, total=10, gaps=0)],
        extra_yaml="publish_policy: human\n",
    )
    try:
        handle = await engine.curate(
            CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")
        )
        job = await handle.wait(timeout=10)
        assert job.status == JobStatus.READY_FOR_APPROVAL
    finally:
        await engine.close()
