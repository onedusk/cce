"""Unified interface for the curation pipeline.

Two modes:
- CurationEngine.embedded(...) — wraps Pipeline, runs in-process
- CurationEngine.remote(base_url, api_key) — HTTP client to the REST API

Both return a JobHandle with the same interface.

Mode dispatch lives in ``curate()`` as a single ``if self._mode == "embedded"``
branch. If a third mode is ever proposed, see ADR-005 in
``docs/decompose/audit-2026-04-14/stage-1-design-pack.md`` before splitting
``CurationEngine`` into subclasses — the current two-mode dispatch was kept
deliberately flat until a concrete third-mode requirement motivates the refactor.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cce.components import build_pipeline
from cce.config.loader import load_config, validate_required_keys
from cce.config.types import EngineConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.models.job import Job, JobError, JobStage, JobStatus
from cce.models.package import PublishPackage
from cce.models.request import CurationRequest
from cce.orchestrator.pipeline import Pipeline
from cce.policy.loader import load_policies
from cce.policy.types import SourcePolicy

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.REVIEW_REQUIRED,
}


async def run_pipeline_task(
    job_id: str,
    request: CurationRequest,
    policy: SourcePolicy,
    *,
    pipeline: Pipeline,
    job_store: JobStore,
    semaphore: asyncio.Semaphore,
    running_tasks: dict[str, asyncio.Task],
) -> None:
    """Shared background pipeline execution.

    Used by both the API route layer and CurationEngine embedded mode.
    Updates job status through the lifecycle, stores the package on success.
    """
    job: Job | None = None
    try:
        async with semaphore:
            job = await job_store.get_job(job_id)
            if job is None:
                return
            job.status = JobStatus.RUNNING
            await job_store.update_job(job)

            result = await pipeline.run(request, policy)

            # Sync pipeline result back to stored job
            job.status = result.job.status
            job.error = result.job.error
            job.stages = result.job.stages
            job.completed_at = result.job.completed_at or datetime.now(UTC)
            await job_store.update_job(job)

            # Store package if produced (remap job_id to our API job ID)
            if result.package:
                package = result.package.model_copy(update={"job_id": job_id})
                await job_store.store_package(job_id, package)

    except asyncio.CancelledError:
        if job is None:
            job = await job_store.get_job(job_id)
        if job:
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(UTC)
            await job_store.update_job(job)
        raise
    except Exception as e:
        logger.exception("Pipeline failed for job %s: %s", job_id, e)
        if job is None:
            job = await job_store.get_job(job_id)
        if job:
            job.status = JobStatus.FAILED
            job.error = JobError(
                code="pipeline_error",
                message=str(e),
                stage=job.stage or JobStage.DISCOVER,
            )
            job.completed_at = datetime.now(UTC)
            await job_store.update_job(job)
    finally:
        running_tasks.pop(job_id, None)


class JobHandle:
    """Handle to a running or completed curation job."""

    def __init__(
        self,
        job_id: str,
        *,
        # Embedded mode deps
        job_store: JobStore | None = None,
        running_tasks: dict[str, asyncio.Task] | None = None,
        engine: CurationEngine | None = None,
        # Remote mode deps
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._job_id = job_id
        self._job_store = job_store
        self._running_tasks = running_tasks
        self._engine = engine
        self._http_client = http_client

    @property
    def job_id(self) -> str:
        return self._job_id

    async def status(self) -> Job:
        """Get current job state."""
        if self._job_store is not None:
            job = await self._job_store.get_job(self._job_id)
            if job is None:
                raise ValueError(f"Job not found: {self._job_id}")
            return job
        assert self._http_client is not None
        resp = await self._http_client.get(f"/v1/curate/jobs/{self._job_id}")
        resp.raise_for_status()
        return Job.model_validate(resp.json()["data"])

    async def package(self) -> PublishPackage | None:
        """Get the completed pipeline output, or None if not yet available."""
        if self._job_store is not None:
            return await self._job_store.get_package(self._job_id)
        assert self._http_client is not None
        resp = await self._http_client.get(f"/v1/curate/jobs/{self._job_id}/package")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return PublishPackage.model_validate(resp.json()["data"])

    async def wait(self, *, poll_interval: float = 0.1, timeout: float = 600) -> Job:
        """Poll until the job reaches a terminal state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            job = await self.status()
            if job.status in _TERMINAL_STATUSES:
                return job
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Job {self._job_id} did not complete within {timeout}s")

    async def cancel(self) -> None:
        """Cancel a running job."""
        if self._running_tasks is not None:
            task = self._running_tasks.pop(self._job_id, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            return
        assert self._http_client is not None
        await self._http_client.delete(f"/v1/curate/jobs/{self._job_id}")

    async def retry(self) -> Job:
        """Re-run the pipeline for this job."""
        if self._engine is not None:
            job = await self.status()
            if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                raise ValueError("Job is already queued or running")
            job.status = JobStatus.QUEUED
            job.error = None
            job.stage = None
            job.completed_at = None
            assert self._job_store is not None
            await self._job_store.update_job(job)
            self._engine._launch_pipeline(job)
            return job
        assert self._http_client is not None
        resp = await self._http_client.post(f"/v1/curate/jobs/{self._job_id}/retry")
        resp.raise_for_status()
        return Job.model_validate(resp.json()["data"])


class CurationEngine:
    """Application facade for the curation pipeline.

    Two construction modes, selected by classmethod:
    ``embedded()`` builds and owns an in-process ``Pipeline`` (used by the
    CLI and runner scripts), and ``remote(base_url, api_key)`` becomes a
    thin HTTP client against a running CCE API server. Both modes expose
    the same ``curate() -> JobHandle`` interface; consumers don't branch.
    """

    def __init__(self) -> None:
        self._mode: str = "uninitialized"
        self._config: EngineConfig | None = None
        self._pipeline: Pipeline | None = None
        self._job_store: JobStore | None = None
        self._evidence_store: SQLiteEvidenceStore | None = None
        self._policies: dict[str, SourcePolicy] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._http_client: httpx.AsyncClient | None = None

    @classmethod
    async def embedded(
        cls,
        config_path: str | None = None,
        policies_dir: str = "policies",
        taxonomies_dir: str = "taxonomies",
        path_configs_path: str | None = None,
    ) -> CurationEngine:
        """Create an in-process engine instance.

        Loads config, builds all components, returns ready-to-use engine.
        """
        engine = cls()
        engine._mode = "embedded"
        engine._config = load_config(config_path)
        validate_required_keys(engine._config)

        # Open stores
        engine._job_store = JobStore(db_path=engine._config.evidence_store.sqlite_path)
        await engine._job_store.connect()

        engine._evidence_store = SQLiteEvidenceStore(engine._config.evidence_store)
        await engine._evidence_store.connect()

        # Build pipeline through the shared component factory (M05, ADR-001)
        engine._pipeline = build_pipeline(engine._config, engine._evidence_store)

        # Load policies
        policies_path = Path(policies_dir)
        if policies_path.exists():
            engine._policies = load_policies(policies_path)

        engine._semaphore = asyncio.Semaphore(engine._config.api.max_concurrent_jobs)

        logger.info(
            "CurationEngine (embedded) ready — %d policies loaded",
            len(engine._policies),
        )
        return engine

    @classmethod
    def remote(
        cls,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> CurationEngine:
        """Create an HTTP client to a running CCE API server.

        ``transport`` is a test seam (audit-2026-06-09 T-04.01): pass
        ``httpx.ASGITransport(app=...)`` to drive an in-process ASGI app.
        The default (None) preserves real HTTP transport.
        """
        engine = cls()
        engine._mode = "remote"
        engine._http_client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            transport=transport,
        )
        return engine

    async def curate(self, request: CurationRequest) -> JobHandle:
        """Submit a curation request and return a job handle."""
        # DEFERRED (audit A2 / ADR-005): split CurationEngine into embedded/
        # remote subclasses when a third mode is proposed. Current two-mode
        # dispatch is deliberately flat.
        # See docs/decompose/audit-2026-04-14/stage-1-design-pack.md.
        if self._mode == "embedded":
            return await self._curate_embedded(request)
        return await self._curate_remote(request)

    async def close(self) -> None:
        """Release resources (DB connections, HTTP clients)."""
        # Cancel running tasks
        for task in list(self._running_tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._running_tasks.clear()

        if self._evidence_store is not None:
            await self._evidence_store.close()
        if self._job_store is not None:
            await self._job_store.close()
        if self._http_client is not None:
            await self._http_client.aclose()

    # -- Private --

    async def _curate_embedded(self, request: CurationRequest) -> JobHandle:
        assert self._pipeline is not None
        assert self._job_store is not None

        policy = self._policies.get(request.policy_id)
        if policy is None:
            raise ValueError(f"Policy not found: {request.policy_id}")

        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            request=request,
        )
        await self._job_store.create_job(job)
        self._launch_pipeline(job)

        return JobHandle(
            job.id,
            job_store=self._job_store,
            running_tasks=self._running_tasks,
            engine=self,
        )

    def _launch_pipeline(self, job: Job) -> None:
        """Launch a background pipeline task for a job."""
        policy = self._policies.get(job.request.policy_id)
        assert policy is not None
        task = asyncio.create_task(self._run_pipeline(job.id, job.request, policy))
        self._running_tasks[job.id] = task

    async def _run_pipeline(
        self,
        job_id: str,
        request: CurationRequest,
        policy: SourcePolicy,
    ) -> None:
        """Background pipeline execution — delegates to shared function."""
        assert self._job_store is not None
        assert self._pipeline is not None
        assert self._semaphore is not None
        await run_pipeline_task(
            job_id,
            request,
            policy,
            pipeline=self._pipeline,
            job_store=self._job_store,
            semaphore=self._semaphore,
            running_tasks=self._running_tasks,
        )

    async def _curate_remote(self, request: CurationRequest) -> JobHandle:
        assert self._http_client is not None
        from cce.api.schemas import JobCreateRequest

        # Map CurationRequest → JobCreateRequest (the API wire format)
        wire = JobCreateRequest(
            topic=request.topic,
            subtopics=request.subtopics,
            paths=request.paths,
            audience=request.audience,
            policy_id=request.policy_id,
            taxonomy_id=request.taxonomy_id,
            path_config_id=request.path_config_id,
            risk_profile=request.risk_profile,
            jurisdiction=(
                request.constraints.jurisdiction if request.constraints else None
            ),
        )
        resp = await self._http_client.post(
            "/v1/curate/jobs",
            json=wire.model_dump(mode="json"),
        )
        resp.raise_for_status()
        job_id = resp.json()["data"]["id"]
        return JobHandle(job_id, http_client=self._http_client)
