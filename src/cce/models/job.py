"""Job tracking data contracts.

A Job tracks the lifecycle of a single curation run through the pipeline.
Phase 1 uses this in-memory; Phase 3 persists it for the API layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field

from cce.models.request import CurationRequest


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVIEW_REQUIRED = "review_required"


class JobStage(StrEnum):
    DISCOVER = "discover"
    EXTRACT = "extract"
    TAG = "tag"
    WRITE = "write"
    VERIFY = "verify"
    PUBLISH = "publish"


class JobError(BaseModel):
    """Error details when a job fails."""

    code: str
    message: str
    stage: JobStage

    model_config = {"frozen": True}


class JobProgress(BaseModel):
    """Progress within the current stage."""

    completed: int = 0
    total: int = 0

    model_config = {"frozen": True}


class StageRecord(BaseModel):
    """Timing record for a completed pipeline stage."""

    stage: JobStage
    started_at: datetime
    completed_at: datetime
    metrics: dict[str, Any] | None = Field(
        default=None,
        description="Optional per-stage metrics. See per-stage TypedDicts below for expected keys.",
    )

    model_config = {"frozen": True}


# --- Per-stage metrics schemas ----------------------------------------------
# StageRecord.metrics stays `dict[str, Any]` for Pydantic flexibility; these
# TypedDicts document the keys each stage is expected to populate. Keys follow
# what the pipeline actually emits today (audit 2026-04-14 M3).


class DiscoverMetrics(TypedDict):
    crawl_success: int
    crawl_failed: int
    crawl_failure_rate: float


class TagMetrics(TypedDict):
    tags_available: bool


class WriteMetrics(TypedDict):
    iterations: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_write: int


class VerifyMetrics(TypedDict):
    total_claims: int
    supported: int
    pass_rate: float
    confidence_score: float


class PublishMetrics(TypedDict):
    token_usage: dict[str, int]


class Job(BaseModel):
    """Tracks a single curation run."""

    id: str
    request: CurationRequest
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage | None = None
    progress: JobProgress | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    error: JobError | None = None
    stages: list[StageRecord] = Field(
        default_factory=list,
        description="Completed stage records for lineage tracking",
    )
