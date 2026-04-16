"""Regression fence: pipeline behavior when discovery yields zero evidence.

Audit T2. Pins the current empty-evidence terminal state so future refactors
can't silently change it:
  - `job.status == JobStatus.FAILED`
  - `job.error` is populated with `code="pipeline_error"`, `stage=DISCOVER`,
    and a message that tells the operator what happened.
  - `package is None` and no ContentUnits / gate results were produced.
  - The LLM was never called (stub would raise if it had been).
  - `job.stages` contains exactly the stage(s) that ran before discovery
    terminated (DISCOVER), and NO write/verify records.

Two scenarios trigger this path: (a) the crawl adapter's search returns
zero URLs; (b) URLs are returned but the SourcePolicy rejects every one.
"""

from __future__ import annotations

import pytest

from cce.models.job import JobStage, JobStatus
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import llm as _llm

pytestmark = pytest.mark.integration


def _assert_empty_evidence_terminal_state(result, pipeline_stage_count: int) -> None:
    """Shared assertions for both empty-evidence scenarios."""
    job = result.job
    # Status + package
    assert job.status == JobStatus.FAILED
    assert result.package is None
    assert result.gate_results == []
    # Error — operator-facing message naming the failure
    assert job.error is not None
    assert job.error.code == "pipeline_error"
    assert job.error.stage == JobStage.DISCOVER
    assert "No evidence discovered" in job.error.message
    # Stages — only DISCOVER ran; no WRITE or VERIFY records
    assert len(job.stages) == pipeline_stage_count
    assert any(s.stage == JobStage.DISCOVER for s in job.stages)
    assert not any(s.stage == JobStage.WRITE for s in job.stages)
    assert not any(s.stage == JobStage.VERIFY for s in job.stages)
    assert not any(s.stage == JobStage.PUBLISH for s in job.stages)


async def test_empty_search_results_terminates_cleanly(sqlite_store):
    """Crawl adapter returns zero URLs → pipeline terminates in DISCOVER."""
    config = make_engine_config()
    adapter = MockCrawlAdapter(search_map={}, url_map={})
    # Empty scripted-response list — if any LLM call is made, MockLLMProvider
    # raises RuntimeError("no more scripted responses"), failing the test.
    llm = _llm()

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    _assert_empty_evidence_terminal_state(result, pipeline_stage_count=1)
    # LLM provider must not have been invoked even once.
    assert llm.calls == []


async def test_strict_policy_rejects_all_urls(sqlite_store):
    """URLs returned by search BUT policy's allowlist rejects every one."""
    config = make_engine_config()

    # Search yields URLs, but they're on example.com — not on the allowlist.
    adapter = MockCrawlAdapter(
        search_map={
            "test topic": ["https://example.com/article", "https://other.com/study"]
        },
        url_map={
            "https://example.com/article": make_crawl_result(
                url="https://example.com/article"
            ),
            "https://other.com/study": make_crawl_result(url="https://other.com/study"),
        },
    )
    llm = _llm()  # empty scripted list — any call fails the test

    # Allow-list containing ONE domain nothing actually matches.
    strict_policy = make_source_policy(
        domains_allow=["impossible-whitelist-domain.example"]
    )

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), strict_policy)

    _assert_empty_evidence_terminal_state(result, pipeline_stage_count=1)
    assert llm.calls == []


async def test_discover_metrics_reflect_no_crawl_on_empty_path(sqlite_store):
    """When no URLs survive, discover metrics are zeroed — not missing."""
    config = make_engine_config()
    adapter = MockCrawlAdapter(search_map={}, url_map={})
    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=_llm(),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    discover_records = [s for s in result.job.stages if s.stage == JobStage.DISCOVER]
    assert len(discover_records) == 1
    metrics = discover_records[0].metrics
    assert metrics is not None
    assert metrics["crawl_success"] == 0
    assert metrics["crawl_failed"] == 0
    assert metrics["crawl_failure_rate"] == 0.0
