"""FastAPI application with lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cce.api.auth import make_auth_dependency
from cce.api.middleware import (
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    install_request_id_log_factory,
)
from cce.api.schemas import envelope
from cce.config.loader import load_config
from cce.config.types import EngineConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.models.job import JobError, JobStage, JobStatus
from cce.orchestrator.pipeline import Pipeline
from cce.policy.types import SourcePolicy

logger = logging.getLogger(__name__)


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
    app.state.semaphore = asyncio.Semaphore(config.api.max_concurrent_jobs)
    app.state.running_tasks: dict[str, asyncio.Task] = {}
    app.state.auth_dependency = auth_dep

    logger.info(
        "CCE API started (policies=%d, max_concurrent=%d, auth=%s)",
        len(policies),
        config.api.max_concurrent_jobs,
        config.api.require_auth,
    )

    yield

    # -- Shutdown --
    SHUTDOWN_TIMEOUT_S = 10

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

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging
    app.add_middleware(RequestLoggingMiddleware)

    # Request ID correlation — registered LAST so it wraps outermost and the
    # contextvar is set before RequestLoggingMiddleware logs the first line.
    app.add_middleware(RequestIdMiddleware)

    # Ensure every log record (from any logger) carries `request_id`.
    install_request_id_log_factory()

    # Global exception handler
    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=envelope(error="Internal server error").model_dump(),
        )

    # Route registration
    from cce.api.routes.evidence import router as evidence_router
    from cce.api.routes.jobs import router as jobs_router
    from cce.api.routes.meta import router as meta_router

    app.include_router(jobs_router)
    app.include_router(evidence_router)
    app.include_router(meta_router)

    return app


# ---------------------------------------------------------------------------
# Internal helpers (production builds only)
# ---------------------------------------------------------------------------


def _build_pipeline(
    config: EngineConfig, evidence_store: SQLiteEvidenceStore
) -> Pipeline:
    """Build the full pipeline from config (mirrors run_live.py wiring)."""
    from cce.discovery.adapters.firecrawl import FirecrawlAdapter
    from cce.discovery.embeddings import EmbeddingUnavailableError
    from cce.discovery.ollama import OllamaEmbeddingProvider
    from cce.llm.anthropic import AnthropicProvider
    from cce.tagging.loader import load_path_configs, load_taxonomy
    from cce.tagging.wellbeing import WellBeingTaxonomy

    crawl_adapter = FirecrawlAdapter(config.crawl)
    llm = AnthropicProvider(config.llm)

    # Embedding provider (optional)
    embedding_provider = None
    if config.embedding.enabled:
        try:
            provider = OllamaEmbeddingProvider(config.embedding)
            embedding_provider = provider
            logger.info("Embedding provider ready: %s", config.embedding.model)
        except (EmbeddingUnavailableError, Exception) as e:
            logger.warning("Embedding provider unavailable: %s", e)

    # Taxonomy plugin (optional)
    taxonomy_plugin = None
    taxonomy_path = Path("taxonomies/wellbeing-8d.yaml")
    if taxonomy_path.exists():
        try:
            taxonomy_config = load_taxonomy(taxonomy_path)
            taxonomy_plugin = WellBeingTaxonomy(taxonomy_config)
            logger.info("Taxonomy loaded: %s", taxonomy_config.name)
        except Exception as e:
            logger.warning("Failed to load taxonomy: %s", e)

    # Path configs (optional)
    path_configs = None
    path_configs_path = Path("path_configs/thnklabs.yaml")
    if path_configs_path.exists():
        try:
            path_configs = load_path_configs(path_configs_path)
            logger.info("Path configs loaded: %s", list(path_configs.keys()))
        except Exception as e:
            logger.warning("Failed to load path configs: %s", e)

    return Pipeline(
        config=config,
        crawl_adapter=crawl_adapter,
        evidence_store=evidence_store,
        llm=llm,
        embedding_provider=embedding_provider,
        taxonomy_plugin=taxonomy_plugin,
        path_configs=path_configs,
    )


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
