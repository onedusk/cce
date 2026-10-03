"""The stored job id is the one the pipeline logs under (OPS-06)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cce.models.job import JobStatus
from cce.models.request import CurationRequest
from tests.test_engine import _make_engine

pytestmark = pytest.mark.integration


async def test_pipeline_logs_carry_the_stored_job_id(
    tmp_path: Path, monkeypatch, caplog
):
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
            handle = await engine.curate(
                CurationRequest(
                    topic="test topic", paths=["blog"], policy_id="test-policy"
                )
            )
            job = await handle.wait(timeout=10)

        assert job.status is JobStatus.COMPLETED
        ids = {r.job_id for r in caplog.records if hasattr(r, "job_id")}
        assert ids == {handle.job_id}
        assert job.updated_at > job.created_at
    finally:
        await engine.close()
