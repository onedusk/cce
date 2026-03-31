"""Evidence retrieval endpoints.

GET /v1/curate/evidence/:evidenceId  — single evidence by ID
GET /v1/curate/evidence              — search evidence store
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from cce.api.schemas import envelope

router = APIRouter(prefix="/v1/curate/evidence", tags=["evidence"])


@router.get("/{evidence_id}")
async def get_evidence(evidence_id: str, request: Request) -> JSONResponse:
    """Get a single evidence object by ID."""
    evidence = await request.app.state.evidence_store.get(evidence_id)
    if evidence is None:
        return JSONResponse(
            status_code=404,
            content=envelope(error=f"Evidence not found: {evidence_id}").model_dump(
                mode="json"
            ),
        )
    return JSONResponse(
        content=envelope(data=evidence.model_dump(mode="json")).model_dump(mode="json")
    )


@router.get("")
async def search_evidence(
    request: Request,
    url: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Search evidence store with optional filters."""
    results = await request.app.state.evidence_store.search(
        url=url, topic=topic, limit=limit
    )
    return JSONResponse(
        content=envelope(
            data=[ev.model_dump(mode="json") for ev in results]
        ).model_dump(mode="json")
    )
