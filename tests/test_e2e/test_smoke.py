"""Key-gated end-to-end smoke test (finding 3.4, T-02.04).

Runs the real pipeline — real Anthropic + Firecrawl — through the embedded
engine against tmp_path stores. Never runs by default: both API keys must be
present in the environment, and CI does not inject them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cce.engine import CurationEngine
from cce.models.job import JobStatus
from cce.models.request import CurationRequest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("FIRECRAWL_API_KEY")),
        reason="e2e smoke requires real API keys",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_single_path_smoke(tmp_path: Path, monkeypatch) -> None:
    """Tiny single-path topic through the embedded engine. Asserts the
    job ends COMPLETED or REVIEW_REQUIRED with a non-empty package.
    Gives the registered e2e marker a real member (finding 3.4)."""
    # Both the evidence store and the job store derive from this path —
    # keeps the run isolated from any local dev databases.
    monkeypatch.setenv("CCE_EVIDENCE_SQLITE_PATH", str(tmp_path / "e2e.db"))

    engine = await CurationEngine.embedded(
        policies_dir=str(_REPO_ROOT / "policies"),
        taxonomies_dir=str(_REPO_ROOT / "taxonomies"),
    )
    try:
        request = CurationRequest(
            topic="benefits of short daily walks",
            paths=["blog"],
            audience="general",
            # Lowest-cost policy in policies/ (max_sources_per_run=15).
            policy_id="peer-reviewed",
            risk_profile="medium",
        )
        handle = await engine.curate(request)
        job = await handle.wait(timeout=600)

        assert job.status in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED}, (
            f"smoke job ended {job.status}: {job.error}"
        )
        package = await handle.package()
        assert package is not None, "terminal job produced no package"
        assert len(package.units) >= 1
    finally:
        await engine.close()
