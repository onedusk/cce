"""Public model exports.

Import the models you need from here:
    from cce.models import Evidence, CurationRequest, ContentUnit, PublishPackage
"""

from __future__ import annotations

from cce.models.content import (
    Citation,
    ClaimMapping,
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence, SourceQuality
from cce.models.job import Job, JobError, JobProgress, JobStage, JobStatus, StageRecord
from cce.models.package import PackageLineage, PublishPackage
from cce.models.paths import PathConfig
from cce.models.request import CurationConstraints, CurationRequest
from cce.models.taxonomy import Dimension, TaxonomyConfig

__all__ = [
    # evidence
    "Evidence",
    "SourceQuality",
    # request
    "CurationRequest",
    "CurationConstraints",
    # content
    "ContentUnit",
    "ContentScores",
    "ContentLineage",
    "Citation",
    "ClaimMapping",
    # job
    "Job",
    "JobStatus",
    "JobStage",
    "JobError",
    "JobProgress",
    "StageRecord",
    # package
    "PublishPackage",
    "PackageLineage",
    # taxonomy (Phase 2)
    "Dimension",
    "TaxonomyConfig",
    # paths (Phase 2)
    "PathConfig",
]
