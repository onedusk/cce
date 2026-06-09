"""FastAPI application with lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cce.api.auth import auth_dependency, make_auth_dependency
from cce.api.middleware import (
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    get_request_id,
    install_body_size_limit,
)
from cce.api.schemas import error_envelope
from cce.components import build_pipeline
from cce.config.loader import ConfigError, load_config, validate_required_keys
from cce.config.types import EngineConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.models.job import JobError, JobStage, JobStatus
from cce.orchestrator.pipeline import Pipeline
from cce.policy.types import SourcePolicy

logger = logging.getLogger(__name__)

# Grace window for in-flight pipeline tasks at shutdown. Module-level so
# tests can monkeypatch it (audit-2026-06-09 T-04.04).
SHUTDOWN_TIMEOUT_S = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup, clean up on shutdown."""
    config: EngineConfig = app.state._config
    overrides: dict[str, Any] = getattr(app.state, "_overrides", {})
    locally_created: set[str] = set()

    # -- JobStore --
    job_store = overrides.get("job_store")
    if job_store is None:
        job_store = JobStore(db_path=config.evidence_store.sqlite_path)
        await job_store.connect()
        locally_created.add("job_store")

    # -- EvidenceStore --
    evidence_store = overrides.get("evidence_store")
    if evidence_store is None:
        evidence_store = SQLiteEvidenceStore(config.evidence_store)
        await evidence_store.connect()
        locally_created.add("evidence_store")

    # -- Pipeline --
    pipeline = overrides.get("pipeline")
    if pipeline is None:
        # Production branch only (tests inject a pipeline): fail fast with
        # the exact env-var name before building components (finding 4.3,
        # ADR-006). SystemExit instead of letting ConfigError propagate —
        # Starlette formats propagated lifespan exceptions as full tracebacks
        # in uvicorn's log; the operator should see one actionable line.
        try:
            validate_required_keys(config)
        except ConfigError as e:
            logger.error("Configuration error: %s", e)
            raise SystemExit(1) from None
        pipeline = _build_pipeline(config, evidence_store)
        locally_created.add("pipeline")

    # -- Policies --
    policies = overrides.get("policies")
    if policies is None:
        policies = _load_policies_safe()

    # -- Auth dependency --
    auth_dep = make_auth_dependency(config.api.require_auth, job_store)

    # -- Populate app.state --
    app.state.config = config
    app.state.pipeline = pipeline
    app.state.job_store = job_store
    app.state.evidence_store = evidence_store
    app.state.policies = policies
    # path_configs is a map {path_name: PathConfig}. Exposed here so
    # create_job can validate `body.paths` against the known names early
    # (audit U2) rather than letting unknown names fail deep in the pipeline.
    # None -> no path_configs loaded -> handler skips the check.
    app.state.path_configs = getattr(pipeline, "_path_configs", None) or None
    app.state.semaphore = asyncio.Semaphore(config.api.max_concurrent_jobs)
    running_tasks: dict[str, asyncio.Task] = {}
    app.state.running_tasks = running_tasks
    app.state.auth_dependency = auth_dep

    logger.info(
        "CCE API started (policies=%d, max_concurrent=%d, auth=%s)",
        len(policies),
        config.api.max_concurrent_jobs,
        config.api.require_auth,
    )

    yield

    # -- Shutdown --

    # 1. Give running tasks time to finish, then hard-cancel stragglers
    tasks = list(app.state.running_tasks.values())
    if tasks:
        logger.info(
            "Shutting down: waiting %ds for %d running task(s)",
            SHUTDOWN_TIMEOUT_S,
            len(tasks),
        )
        done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # 2. Mark orphaned RUNNING jobs as FAILED
    if hasattr(app.state, "job_store") and app.state.job_store is not None:
        running_jobs = await app.state.job_store.list_jobs(
            status=JobStatus.RUNNING, limit=1000
        )
        for job in running_jobs:
            job.status = JobStatus.FAILED
            job.error = JobError(
                code="server_shutdown",
                message="Server shut down while job was running",
                stage=job.stage or JobStage.DISCOVER,
            )
            await app.state.job_store.update_job(job)
        if running_jobs:
            logger.info(
                "Marked %d orphaned RUNNING job(s) as FAILED", len(running_jobs)
            )

    if "evidence_store" in locally_created:
        await evidence_store.close()
    if "job_store" in locally_created:
        await job_store.close()

    logger.info("CCE API shut down")


def create_app(
    config: EngineConfig | None = None,
    *,
    job_store: JobStore | None = None,
    evidence_store: SQLiteEvidenceStore | None = None,
    pipeline: Pipeline | None = None,
    policies: dict[str, SourcePolicy] | None = None,
) -> FastAPI:
    """Factory for the FastAPI application.

    In production, call with no args — config is loaded from env/YAML
    and all components are built by the lifespan.

    In tests, pass pre-built components to inject mocks.
    """
    if config is None:
        config = load_config()

    app = FastAPI(
        title="Content Curation Engine",
        version=config.engine_version,
        lifespan=lifespan,
    )

    # Store config and overrides for the lifespan to consume
    app.state._config = config
    app.state._overrides = {
        k: v
        for k, v in {
            "job_store": job_store,
            "evidence_store": evidence_store,
            "pipeline": pipeline,
            "policies": policies,
        }.items()
        if v is not None
    }

    # Body-size limit (finding 5.1) — registered first so CORS (and the
    # request-ID/logging middlewares) wrap it and the 413 envelope still
    # carries CORS headers and a correlation ID.
    install_body_size_limit(app)

    # CORS — defensive: if origins is wildcard, credentials must be off.
    # Starlette's CORSMiddleware otherwise reflects the inbound Origin back
    # alongside `Access-Control-Allow-Credentials: true`, defeating the
    # browser's wildcard-vs-credentials safety rule. Bearer-token auth
    # doesn't travel on cross-origin fetches today (no exploit), but this
    # closes the door if cookie auth is ever added. See docs/security/.
    cors_allow_credentials = "*" not in config.api.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging
    app.add_middleware(RequestLoggingMiddleware)

    # Request ID correlation — registered LAST so it wraps outermost and the
    # contextvar is set before RequestLoggingMiddleware logs the first line.
    app.add_middleware(RequestIdMiddleware)

    # Ensure every log record (from any logger) carries `request_id` and
    # apply the JSON formatter if CCE_LOG_FORMAT=json is set. Idempotent.
    from cce.logging_config import configure_logging

    configure_logging()

    # Global exception handler
    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                code="internal_error",
                message="Internal server error",
                request_id=get_request_id(),
            ).model_dump(),
        )

    # Route registration
    from cce.api.routes.evidence import router as evidence_router
    from cce.api.routes.jobs import router as jobs_router
    from cce.api.routes.meta import router as meta_router

    app.include_router(jobs_router, dependencies=[Depends(auth_dependency)])
    app.include_router(evidence_router, dependencies=[Depends(auth_dependency)])
    app.include_router(meta_router)  # intentionally unauthed (health, meta)

    return app


# ---------------------------------------------------------------------------
# Internal helpers (production builds only)
# ---------------------------------------------------------------------------


def _build_pipeline(
    config: EngineConfig, evidence_store: SQLiteEvidenceStore
) -> Pipeline:
    """Thin shim over the shared component factory (audit-2026-06-09 M05).

    Wiring authority lives in ``cce.components`` (ADR-001, finding 1.1); this
    name is kept so existing references (factory-level tests) survive the move.
    """
    return build_pipeline(config, evidence_store)


def _load_policies_safe() -> dict[str, SourcePolicy]:
    """Load all policies from the policies/ directory, or return empty dict."""
    from cce.policy.loader import load_policies

    policies_dir = Path("policies")
    if policies_dir.exists():
        try:
            return load_policies(policies_dir)
        except Exception as e:
            logger.warning("Failed to load policies: %s", e)
    return {}
