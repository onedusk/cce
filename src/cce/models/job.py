"""Job tracking data contracts.

A Job tracks the lifecycle of a single curation run through the pipeline.
Phase 1 uses this in-memory; Phase 3 persists it for the API layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        pass
from typing import Optional

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

    model_config = {"frozen": True}


class Job(BaseModel):
    """Tracks a single curation run."""

    id: str
    request: CurationRequest
    status: JobStatus = JobStatus.QUEUED
    stage: Optional[JobStage] = None
    progress: Optional[JobProgress] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    error: Optional[JobError] = None
    stages: list[StageRecord] = Field(
        default_factory=list,
        description="Completed stage records for lineage tracking",
    )
