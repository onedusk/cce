"""API request/response schemas.

These are the HTTP-facing contracts. Internal models (Job, PublishPackage)
are serialized through these schemas, not exposed directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from cce.models.job import Job


# ---------------------------------------------------------------------------
# Standard envelope
# ---------------------------------------------------------------------------


class APIEnvelope(BaseModel):
    """Standard response wrapper."""

    data: Any = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def envelope(
    data: Any = None,
    *,
    error: str | None = None,
    **meta: Any,
) -> APIEnvelope:
    """Build an APIEnvelope with an auto-generated timestamp."""
    meta_dict: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    meta_dict.update(meta)
    return APIEnvelope(data=data, error=error, meta=meta_dict)


# ---------------------------------------------------------------------------
# Job schemas
# ---------------------------------------------------------------------------


class JobResponse(BaseModel):
    """Job state as returned by the API."""

    id: str
    status: str
    topic: str
    policy_id: str
    stage: str | None = None
    progress: dict[str, int] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None


class JobListResponse(BaseModel):
    """Paginated job list."""

    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int


class JobCreateRequest(BaseModel):
    """Request body for POST /v1/curate/jobs.

    Maps directly to CurationRequest fields.
    """

    topic: str
    subtopics: list[str] = Field(default_factory=list)
    paths: list[str]
    audience: str = "general"
    policy_id: str
    taxonomy_id: str | None = None
    path_config_id: str | None = None
    risk_profile: str = "medium"
    jurisdiction: str | None = None


# ---------------------------------------------------------------------------
# Package / evidence schemas
# ---------------------------------------------------------------------------


class PackageResponse(BaseModel):
    """Abbreviated package metadata (full package via dedicated endpoint)."""

    job_id: str
    units: int
    evidence_count: int
    confidence: float
    coverage: float
    diversity: float


# ---------------------------------------------------------------------------
# Health / meta schemas
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Health check response."""

    status: str  # "ok" or "degraded"
    db_reachable: bool
    engine_version: str


class MetaResponse(BaseModel):
    """Service metadata."""

    engine_version: str
    policies: list[str]
    taxonomies: list[str]
    path_configs: list[str]
    queue_depth: int
    adapters: dict[str, str]


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def job_to_response(job: Job) -> JobResponse:
    """Convert an internal Job model to the API-facing JobResponse."""
    return JobResponse(
        id=job.id,
        status=job.status.value,
        topic=job.request.topic,
        policy_id=job.request.policy_id,
        stage=job.stage.value if job.stage else None,
        progress=job.progress.model_dump() if job.progress else None,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
        error=job.error.model_dump() if job.error else None,
    )
