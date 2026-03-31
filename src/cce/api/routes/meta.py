"""Health and metadata endpoints.

GET /v1/curate/health  — service health check
GET /v1/curate/meta    — engine metadata
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cce.api.schemas import HealthResponse, MetaResponse, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/curate", tags=["meta"])


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Service health check. No auth required."""
    state = request.app.state
    db_reachable = await state.job_store.ping()

    status = "ok" if db_reachable else "degraded"
    return JSONResponse(
        content=envelope(
            data=HealthResponse(
                status=status,
                db_reachable=db_reachable,
                engine_version=state.config.engine_version,
            ).model_dump(mode="json")
        ).model_dump(mode="json")
    )


@router.get("/meta")
async def meta(request: Request) -> JSONResponse:
    """Service metadata — loaded config, policies, queue depth."""
    state = request.app.state
    config = state.config

    return JSONResponse(
        content=envelope(
            data=MetaResponse(
                engine_version=config.engine_version,
                policies=sorted(state.policies.keys()),
                taxonomies=[],  # populated when taxonomy registry exists
                path_configs=[],  # populated when path config registry exists
                queue_depth=len(state.running_tasks),
                adapters={
                    "crawl": config.crawl.adapter,
                    "llm": config.llm.provider,
                    "embedding": config.embedding.provider
                    if config.embedding.enabled
                    else "disabled",
                },
            ).model_dump(mode="json")
        ).model_dump(mode="json")
    )
