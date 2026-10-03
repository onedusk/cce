"""Pipeline.run takes the caller's job id for its Job, logs and package (OPS-06)."""

from __future__ import annotations

import logging

import pytest

from cce.orchestrator.pipeline import Pipeline
from tests.conftest import make_curation_request, make_engine_config, make_source_policy
from tests.test_orchestrator.conftest import (
    llm,
    make_adapter,
    verifier_json,
    writer_json,
)

pytestmark = pytest.mark.integration


def _pipeline(sqlite_store) -> Pipeline:
    return Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm(writer_json(), verifier_json()),
    )


async def test_run_uses_the_callers_job_id(sqlite_store, caplog):
    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await _pipeline(sqlite_store).run(
            make_curation_request(), make_source_policy(), job_id="job_caller000001"
        )

    assert result.job.id == "job_caller000001"
    assert result.package is not None
    assert result.package.job_id == "job_caller000001"
    ids = {r.job_id for r in caplog.records if hasattr(r, "job_id")}
    assert ids == {"job_caller000001"}


async def test_run_mints_a_job_id_when_none_is_given(sqlite_store):
    result = await _pipeline(sqlite_store).run(
        make_curation_request(), make_source_policy()
    )
    assert result.job.id.startswith("job_")
