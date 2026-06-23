"""Tests for the per-job token budget checkpoint (M08, T-08.02 — ADR-003).

A scripted fake LLM with known per-call usage drives the write-verify loop;
the budget checkpoint at the top of each writer iteration must stop the loop,
keep the prior iteration's draft, and route the job to REVIEW_REQUIRED.
With ``max_tokens_per_job=None`` (the default) outcomes are identical to the
pre-budget pipeline — pinned by parameterizing a multi-iteration scenario
over the budget.
"""

import logging

import pytest

from cce.llm.base import LLMResponse
from cce.models.job import JobStatus
from cce.orchestrator.pipeline import Pipeline
from cce.verification.gate import GateDecision
from tests.conftest import (
    MockLLMProvider,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    make_adapter as _make_adapter,
)
from tests.test_orchestrator.conftest import (
    verifier_json as _verifier_json,
)
from tests.test_orchestrator.conftest import (
    writer_json as _writer_json,
)

WRITER_USAGE = {"input_tokens": 1200, "output_tokens": 300}
VERIFIER_USAGE = {"input_tokens": 400, "output_tokens": 100}
# One full iteration = writer + verifier = 2000 input+output tokens.
ONE_ITERATION_TOKENS = 2000


def _llm_with_usage(*scripted: tuple[str, dict]) -> MockLLMProvider:
    """Scripted LLM where each response carries a known usage dict."""
    return MockLLMProvider(
        [
            LLMResponse(
                content=content, model="mock", usage=usage, stop_reason="end_turn"
            )
            for content, usage in scripted
        ]
    )


# ---------------------------------------------------------------------------
# _budget_exceeded — pure logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_exceeded_none_is_unlimited():
    assert Pipeline._budget_exceeded({"input_tokens": 10**9}, None) is False


@pytest.mark.unit
def test_budget_exceeded_below_at_and_above_threshold():
    usage = {"input_tokens": 1500, "output_tokens": 500}
    assert Pipeline._budget_exceeded(usage, 2001) is False
    assert Pipeline._budget_exceeded(usage, 2000) is True  # breach is inclusive
    assert Pipeline._budget_exceeded(usage, 1999) is True


@pytest.mark.unit
def test_budget_exceeded_missing_keys_count_as_zero():
    assert Pipeline._budget_exceeded({}, 1) is False
    assert Pipeline._budget_exceeded({"cache_read_input_tokens": 10**6}, 1) is False


# ---------------------------------------------------------------------------
# Budget breach mid-job → REVIEW_REQUIRED with the partial draft kept
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_budget_breach_stops_at_iteration_boundary(sqlite_store, caplog):
    """Budget below 2 iterations: the loop stops at the iteration-2 boundary,
    the job routes to REVIEW_REQUIRED, and the iteration-1 draft is retained
    in the package (ADR-003 — never discard paid-for work)."""
    config = make_engine_config(max_tokens_per_job=ONE_ITERATION_TOKENS)
    adapter = _make_adapter()
    # Iteration 1 FAILs the gate (low confidence, fixable issues), which
    # would normally trigger a rewrite — only 2 responses are scripted, so
    # any second writer call would raise inside MockLLMProvider and fail
    # the job. Reaching REVIEW_REQUIRED proves the checkpoint stopped first.
    llm = _llm_with_usage(
        (_writer_json(content="Draft v1 [ev:ev_001]."), WRITER_USAGE),
        (_verifier_json(supported=3, total=10, unsupported=5, gaps=2), VERIFIER_USAGE),
    )

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    with caplog.at_level(logging.WARNING):
        result = await pipeline.run(make_curation_request(), make_source_policy())

    # Stopped at the iteration-2 boundary: exactly one writer+verifier pair ran.
    assert len(llm.calls) == 2
    assert result.job.status == JobStatus.REVIEW_REQUIRED

    # Iteration-1 draft retained in the package.
    assert result.package is not None
    assert len(result.package.units) == 1
    assert "Draft v1" in result.package.units[0].content

    # Budget note rides on the last (FAIL) gate result's feedback channel.
    assert len(result.gate_results) == 1
    assert result.gate_results[0].decision == GateDecision.FAIL
    assert "Token budget exceeded" in result.gate_results[0].feedback

    # Persisted note for `cce status`: a stage record carries the breach.
    budget_records = [
        rec
        for rec in result.job.stages
        if rec.metrics and rec.metrics.get("budget_exceeded")
    ]
    assert len(budget_records) == 1
    assert budget_records[0].path == "blog"
    assert budget_records[0].metrics is not None
    assert budget_records[0].metrics["tokens_spent"] == ONE_ITERATION_TOKENS
    assert budget_records[0].metrics["max_tokens_per_job"] == ONE_ITERATION_TOKENS

    # One warning with spent/budget numbers.
    warnings = [r for r in caplog.records if "token budget exceeded" in r.getMessage()]
    assert len(warnings) == 1
    assert str(ONE_ITERATION_TOKENS) in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# Cross-path accumulation: the cap spans paths (ADR-003 "all paths"). Under
# sequential execution (M03) a later path's checkpoint sees earlier paths'
# spend — untestable under the old concurrent model where a path could not
# observe its siblings' spend within the gather.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_budget_accumulates_across_paths_sequentially(sqlite_store):
    """A budget that path 1 alone does not breach stops path 2 before its
    writer, because path 2's checkpoint sees path 1's already-merged spend."""
    config = make_engine_config(max_tokens_per_job=ONE_ITERATION_TOKENS)
    adapter = _make_adapter()
    # Only path 1 ("blog") calls the LLM: writer1 PASSes the gate in one
    # iteration (2000 tokens). Path 2 ("summary") breaches at its iteration-1
    # checkpoint (cumulative 2000 >= budget) BEFORE any writer call, so no
    # responses are scripted for it — a stray call would raise in MockLLMProvider.
    llm = _llm_with_usage(
        (_writer_json(content="Blog draft [ev:ev_001]."), WRITER_USAGE),
        (_verifier_json(supported=10, total=10, gaps=0), VERIFIER_USAGE),
    )

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(
        make_curation_request(paths=["blog", "summary"]), make_source_policy()
    )

    # Only path 1 ran the LLM; path 2 stopped before its writer.
    assert len(llm.calls) == 2
    # Path 1 produced a draft; path 2 produced none (stopped pre-writer).
    assert result.package is not None
    assert [u.path for u in result.package.units] == ["blog"]
    # The job did not complete cleanly — the cumulative cap bit on path 2.
    assert result.job.status != JobStatus.COMPLETED

    # The breach is recorded against the SECOND path, driven by cumulative spend
    # (the spend came entirely from path 1).
    budget_records = [
        rec
        for rec in result.job.stages
        if rec.metrics and rec.metrics.get("budget_exceeded")
    ]
    assert len(budget_records) == 1
    assert budget_records[0].path == "summary"
    assert budget_records[0].metrics["stopped_before_iteration"] == 1
    assert budget_records[0].metrics["tokens_spent"] == ONE_ITERATION_TOKENS


# ---------------------------------------------------------------------------
# Regression pin: budget None (and a budget that never breaches) leave the
# multi-iteration rewrite scenario untouched
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("budget", [None, 10**9])
async def test_budget_none_or_unreached_is_behavior_neutral(sqlite_store, budget):
    """The existing rewrite-loop scenario (FAIL then PASS) parameterized over
    the budget: outcomes are identical to the pre-budget pipeline."""
    config = make_engine_config(max_tokens_per_job=budget)
    adapter = _make_adapter()
    llm = _llm_with_usage(
        (_writer_json(content="Draft v1 [ev:ev_001]."), WRITER_USAGE),
        (_verifier_json(supported=3, total=10, unsupported=5, gaps=2), VERIFIER_USAGE),
        (_writer_json(content="Draft v2 [ev:ev_001]."), WRITER_USAGE),
        (_verifier_json(supported=10, total=10, gaps=0), VERIFIER_USAGE),
    )

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert result.job.status == JobStatus.COMPLETED
    assert len(llm.calls) == 4  # writer1, verifier1, writer2, verifier2
    assert [gr.decision for gr in result.gate_results] == [
        GateDecision.FAIL,
        GateDecision.PASS,
    ]
    assert result.package is not None
    assert "Draft v2" in result.package.units[0].content

    # No budget note anywhere — feedback or stage records.
    assert all("budget" not in gr.feedback for gr in result.gate_results)
    assert not any(
        rec.metrics and rec.metrics.get("budget_exceeded") for rec in result.job.stages
    )
