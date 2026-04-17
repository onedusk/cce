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
    SCORE = "score"  # Humanization M02 — programmatic style scorer
    EDIT = "edit"  # Humanization M03 — stylistic rewrite (conditional on SCORE fail)
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
    path: str
    iterations: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_write: int


class VerifyMetrics(TypedDict):
    path: str
    total_claims: int
    supported: int
    pass_rate: float
    confidence_score: float


class PublishMetrics(TypedDict):
    token_usage: dict[str, int]


class ScoreMetrics(TypedDict):
    """Per-iteration output of the programmatic style scorer (humanization M02)."""

    path: str
    sentence_length_stddev: float
    suppressed_vocab_hits: int
    type_token_ratio: float
    formulaic_transition_count: int
    contrastive_frame_count: int
    hedging_phrase_count: int
    word_count: int
    humanization_pass: bool


class EditMetrics(TypedDict):
    """Per-invocation output of the Editor agent (humanization M03).

    ``invoked`` is always True when this record is present — absence of the
    record means the scorer passed and the editor was skipped. ``citations_preserved``
    False indicates the editor's output dropped or added an [ev:ID] marker and
    the writer's original draft was retained for the verifier.
    """

    path: str
    invoked: bool
    citations_preserved: bool
    word_count_before: int
    word_count_after: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_write: int


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
