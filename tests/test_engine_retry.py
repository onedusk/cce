"""Embedded-mode retry lifecycle: a retry starts from a clean slate (COR-02)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cce.models.job import JobStatus
from cce.models.request import CurationRequest
from tests.test_engine import _make_engine

pytestmark = pytest.mark.integration


def _request() -> CurationRequest:
    return CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")


async def test_failed_retry_does_not_serve_the_previous_package(
    tmp_path: Path, monkeypatch
):
    """COR-02: while a retry runs and after it fails, the job has no package
    and none of the previous run's stage records."""
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await engine.curate(_request())
        first = await handle.wait(timeout=10)
        assert first.status is JobStatus.COMPLETED
        assert first.stages
        assert await handle.package() is not None

        release = asyncio.Event()

        async def _fail_later(request, policy, **kwargs):
            await release.wait()
            raise RuntimeError("retry failed")

        monkeypatch.setattr(engine._pipeline, "run", _fail_later)

        requeued = await handle.retry()
        assert requeued.stages == []
        assert await handle.package() is None
        assert (await handle.status()).stages == []

        release.set()
        final = await handle.wait(timeout=10)
        assert final.status is JobStatus.FAILED
        assert await handle.package() is None
    finally:
        await engine.close()


async def test_retry_with_unknown_policy_leaves_job_untouched(
    tmp_path: Path, monkeypatch
):
    """A retry whose policy is gone raises before any write: the job keeps its
    status and package instead of being left QUEUED with no task."""
    engine = await _make_engine(tmp_path, monkeypatch)
    try:
        handle = await engine.curate(_request())
        assert (await handle.wait(timeout=10)).status is JobStatus.COMPLETED

        monkeypatch.setattr(engine, "_policies", {})
        with pytest.raises(ValueError, match="Policy not found"):
            await handle.retry()

        assert (await handle.status()).status is JobStatus.COMPLETED
        assert await handle.package() is not None
    finally:
        await engine.close()
