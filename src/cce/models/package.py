"""Publish package data contracts.

The PublishPackage is the engine's final output -- everything a consuming
platform needs to display, index, and audit the curated content.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cce.models.content import ContentScores, ContentUnit
from cce.models.evidence import Evidence
from cce.models.job import StageRecord
from cce.models.verification import PathVerification


class PackageLineage(BaseModel):
    """Full provenance for the curation run that produced this package."""

    policy_id: str
    taxonomy_id: str = ""
    path_config_id: str = ""
    run_id: str
    engine_version: str
    stages: list[StageRecord] = Field(
        default_factory=list,
        description="Timing records for each completed pipeline stage",
    )

    model_config = {"frozen": True}


class PublishPackage(BaseModel):
    """The engine's complete output for a curation run."""

    job_id: str
    units: list[ContentUnit] = Field(description="One ContentUnit per output path")
    evidence: list[Evidence] = Field(
        description="All evidence objects used across all units"
    )
    scores: ContentScores = Field(description="Aggregate scores across all units")
    lineage: PackageLineage
    verification: list[PathVerification] = Field(
        default_factory=list,
        description=(
            "Per requested path: the terminal gate decision and feedback, the "
            "verifier's per-claim verdicts and the writer's gaps (B7). Empty "
            "on packages stored before B7."
        ),
    )

    model_config = {"frozen": True}
