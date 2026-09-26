"""Persisted verification results (B7).

A reviewer spot-checks a package instead of rereading it: per path, the
terminal gate decision and its feedback, the verifier's per-claim verdicts,
and the writer's declared gaps. Frozen snapshots of the runtime dataclasses
in ``verification/`` (which stay mutable for the pipeline's own use); the
raw reply text and token usage are deliberately not persisted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClaimVerdict(BaseModel):
    """The verifier's assessment of one claim."""

    claim: str
    citation_ids: list[str] = Field(default_factory=list)
    # A plain str: the assessment vocabulary is enforced only when the
    # provider honours the structured-output schema.
    assessment: str = ""
    explanation: str = ""
    suggestion: str = ""

    model_config = {"frozen": True}


class SourceContradiction(BaseModel):
    """Two or more sources the verifier found disagreeing."""

    topic: str
    evidence_ids: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class VerificationRecord(BaseModel):
    """One verifier report, without the raw reply or token usage."""

    claims: list[ClaimVerdict] = Field(default_factory=list)
    total_claims: int = 0
    supported: int = 0
    unsupported: int = 0
    uncited: int = 0
    leakage: int = 0
    conflicts: int = 0
    gaps_acknowledged: int = 0
    contradictions: list[SourceContradiction] = Field(default_factory=list)
    overall_feedback: str = ""
    confidence_score: float = 0.0

    model_config = {"frozen": True}


class PathVerification(BaseModel):
    """The terminal verification outcome for one requested output path.

    ``decision`` is the quality gate's call, independent of any publish
    policy. ``unit_id`` links to ``PublishPackage.units``; it is None when
    the path produced no unit (empty draft, or stopped before a write), and
    then ``report`` is None too.
    """

    path: str
    unit_id: str | None = None
    decision: Literal["pass", "fail", "review"]
    iteration: int | None = None
    confidence: float = 0.0
    coverage: float = 0.0
    feedback: str = ""
    report: VerificationRecord | None = None
    writer_gaps: list[str] = Field(
        default_factory=list,
        description="Gaps the writer declared for the draft this record describes",
    )

    model_config = {"frozen": True}
