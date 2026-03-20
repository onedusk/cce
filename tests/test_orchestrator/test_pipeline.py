"""Tests for cce.orchestrator.pipeline — full pipeline orchestration."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.models.job import Job, JobStatus
from cce.orchestrator.pipeline import Pipeline, PipelineResult
from cce.verification.gate import GateDecision
from tests.conftest import (
    MockCrawlAdapter,
    MockLLMProvider,
    make_crawl_result,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)


# ---------------------------------------------------------------------------
# Helpers — scripted LLM JSON responses
# ---------------------------------------------------------------------------


def _writer_json(*, content: str = "Draft with citation [ev:ev_001].") -> str:
    return json.dumps(
        {
            "content": content,
            "citations_used": ["ev_001"],
            "evidence_map": [
                {"claim": "Draft claim", "evidence_ids": ["ev_001"]}
            ],
            "gaps": [],
        }
    )


def _verifier_json(
    *,
    supported: int = 8,
    total: int = 10,
    leakage: int = 0,
    conflicts: int = 0,
    unsupported: int = 0,
    uncited: int = 0,
    gaps: int = 2,
) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim": f"Claim {i}",
                    "citation_ids": ["ev_001"],
                    "assessment": "supported",
                    "explanation": "OK",
                    "suggestion": "",
                }
                for i in range(supported)
            ],
            "summary": {
                "total_claims": total,
                "supported": supported,
                "unsupported": unsupported,
                "uncited": uncited,
                "leakage": leakage,
                "conflicts": conflicts,
                "gaps_acknowledged": gaps,
            },
            "overall_feedback": "All good.",
            "contradictions": [],
        }
    )


def _make_adapter():
    """Standard adapter with one search result and one crawl result."""
    return MockCrawlAdapter(
        search_map={
            "test topic": ["https://example.com/article"],
        },
        url_map={
            "https://example.com/article": make_crawl_result(
                url="https://example.com/article",
                markdown=(
                    "This is a substantial paragraph with real content that exceeds "
                    "the fifty character minimum for evidence extraction in tests."
                ),
            ),
        },
    )


def _llm(*json_strings: str) -> MockLLMProvider:
    return MockLLMProvider(
        [LLMResponse(content=s, model="mock", stop_reason="end_turn") for s in json_strings]
    )


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pipeline_happy_path(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(_writer_json(), _verifier_json())

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert result.package is not None
    assert len(result.package.units) == 1
    assert result.job.status == JobStatus.COMPLETED


@pytest.mark.integration
async def test_pipeline_no_evidence(sqlite_store):
    config = make_engine_config()
    # Empty search results → no evidence discovered
    adapter = MockCrawlAdapter(search_map={}, url_map={})
    llm = _llm()  # no LLM calls expected

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.failed is True
    assert result.job.status == JobStatus.FAILED


@pytest.mark.integration
async def test_pipeline_single_pass(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    # High confidence → PASS on first iteration
    llm = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert len(result.gate_results) == 1
    assert result.gate_results[0].decision == GateDecision.PASS


@pytest.mark.integration
async def test_pipeline_rewrite_loop(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    # Round 1: low confidence + fixable issues → FAIL
    # Round 2: high confidence → PASS
    llm = _llm(
        _writer_json(content="Draft v1 [ev:ev_001]."),
        _verifier_json(supported=3, total=10, unsupported=5, gaps=2),
        _writer_json(content="Draft v2 [ev:ev_001]."),
        _verifier_json(supported=10, total=10, gaps=0),
    )

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert len(result.gate_results) >= 2
    # Second writer call (index 2) should have received feedback
    assert len(llm.calls) == 4  # writer1, verifier1, writer2, verifier2
    second_writer_prompt = llm.calls[2]["messages"][0].content
    assert "VERIFIER FEEDBACK" in second_writer_prompt


@pytest.mark.integration
@pytest.mark.slow
async def test_pipeline_review_max_iterations(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    # Every iteration: low confidence + fixable → FAIL until max, then REVIEW
    # max_writer_iterations=3 for medium profile
    responses = []
    for _ in range(3):
        responses.append(_writer_json())
        responses.append(_verifier_json(supported=3, total=10, unsupported=5, gaps=2))
    llm = _llm(*responses)

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.needs_review is True
    assert result.job.status == JobStatus.REVIEW_REQUIRED


@pytest.mark.integration
async def test_pipeline_multiple_paths(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    # 2 paths → 2 writer+verifier rounds
    llm = _llm(
        _writer_json(content="Blog content [ev:ev_001]."),
        _verifier_json(),
        _writer_json(content="Newsletter content [ev:ev_001]."),
        _verifier_json(),
    )

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    request = make_curation_request(paths=["blog", "newsletter"])
    result = await pipeline.run(request, make_source_policy())

    assert result.package is not None
    assert len(result.package.units) == 2
    paths = {u.path for u in result.package.units}
    assert paths == {"blog", "newsletter"}


@pytest.mark.integration
async def test_pipeline_exception_handling(sqlite_store):
    config = make_engine_config()

    class FailingAdapter(MockCrawlAdapter):
        async def search(self, query: str, limit: int = 10) -> list[str]:
            raise RuntimeError("Network error")

    adapter = FailingAdapter()
    llm = _llm()

    pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm)
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.failed is True
    assert result.job.error is not None
    assert "Network error" in result.job.error.message


# ---------------------------------------------------------------------------
# _update_job — unit test (sync)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_job_status_transitions():
    request = make_curation_request()
    job = Job(id="job_test", request=request)
    assert job.status == JobStatus.QUEUED

    job = Pipeline._update_job(job, JobStatus.COMPLETED)
    assert job.status == JobStatus.COMPLETED
    assert job.completed_at is not None

    job2 = Job(id="job_test2", request=request)
    job2 = Pipeline._update_job(job2, JobStatus.FAILED, error_msg="oops")
    assert job2.status == JobStatus.FAILED
    assert job2.error is not None
    assert job2.error.message == "oops"
