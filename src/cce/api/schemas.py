"""API request/response schemas.

These are the HTTP-facing contracts. Internal models (Job, PublishPackage)
are serialized through these schemas, not exposed directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from cce.models.job import Job

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Standard envelope
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """Structured error payload for APIEnvelope.error (audit U1).

    Fields:
      - ``code``: short machine-readable identifier clients can switch on
        (e.g. ``"not_found"``, ``"internal_error"``).
      - ``message``: optional human-readable detail.
      - ``request_id``: correlation ID set by RequestIdMiddleware; absent
        on synthetic envelopes constructed outside a request.
    """

    code: str
    message: str | None = None
    request_id: str | None = None


class APIEnvelope(BaseModel, Generic[T]):
    """Standard response wrapper.

    Generic over T so that ``APIEnvelope[JobResponse]`` produces a typed
    ``data`` field in the OpenAPI schema.

    Note: the ``error`` field's shape changed from ``str`` to ``ErrorBody``
    in the audit-2026-04-14 remediation. Callers migrating from the old
    string form: read ``response.error.message`` instead of
    ``response.error``. Clients can also switch on ``response.error.code``.
    """

    data: T | None = None
    error: ErrorBody | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def envelope(
    data: Any = None,
    *,
    error: ErrorBody | None = None,
    **meta: Any,
) -> APIEnvelope:
    """Build an APIEnvelope with an auto-generated timestamp."""
    meta_dict: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
    }
    meta_dict.update(meta)
    return APIEnvelope(data=data, error=error, meta=meta_dict)


def error_envelope(
    code: str,
    *,
    message: str | None = None,
    request_id: str | None = None,
    **meta: Any,
) -> APIEnvelope:
    """Build an APIEnvelope wrapping a structured ErrorBody.

    Preferred over ``envelope(error=ErrorBody(...))`` for all error paths —
    it's shorter and makes the code value a required, named argument so
    call sites have to choose one deliberately.
    """
    return envelope(
        error=ErrorBody(code=code, message=message, request_id=request_id),
        **meta,
    )


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
