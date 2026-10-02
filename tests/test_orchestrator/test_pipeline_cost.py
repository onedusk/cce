"""Per-job cost estimate and implied-claim token accounting (B15, audit 5.1).

One run where the writer, the implied-claim checker, the editor and the
verifier answer on three models. The expected cost is worked by hand from
the packaged list prices, so a change to how usage is attributed to models
shows up as a wrong number.
"""

from __future__ import annotations

import logging

import pytest

from cce.config.pricing import load_model_pricing
from cce.llm.base import LLMResponse
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockLLMProvider,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import verifier_json as _verifier_json
from tests.test_orchestrator.test_pipeline_implied_claims import (
    _ai_flat_with_contrast,
    _checker,
    _editor,
    _editor_response,
    _make_adapter_with_markdown,
    _scorer,
    _topic_extract_response,
)

pytestmark = pytest.mark.integration

WRITER = (
    "claude-sonnet-5",
    {"input_tokens": 1000, "output_tokens": 2000, "cache_creation_input_tokens": 3000},
)
TOPIC = ("claude-sonnet-5", {"input_tokens": 100, "output_tokens": 50})
EDITOR = ("claude-haiku-4-5-20251001", {"input_tokens": 500, "output_tokens": 1500})
VERIFIER = (
    "claude-opus-5",
    {"input_tokens": 200, "output_tokens": 800, "cache_read_input_tokens": 3000},
)
# Per million: sonnet-5 2 / 10 / 2.50 write; haiku-4-5 1 / 5; opus-5 5 / 25 / 0.50 read.
EXPECTED_USD = (
    (1000 * 2 + 2000 * 10 + 3000 * 2.5)  # writer
    + (100 * 2 + 50 * 10)  # implied-claim topic call
    + (500 * 1 + 1500 * 5)  # editor
    + (200 * 5 + 800 * 25 + 3000 * 0.5)  # verifier
) / 1_000_000
REWRITTEN = (
    "Sleep matters [ev:ev_001]. Sleeping pills can help in acute insomnia, "
    "but they don't change the habits that keep the problem coming back."
)


def _llm(
    verifier_model: str = VERIFIER[0], verdict: str | None = None
) -> MockLLMProvider:
    """Call order: writer, topic extractor, editor, verifier."""
    verdict = verdict or _verifier_json(supported=10, total=10, gaps=0)
    script = [
        (_ai_flat_with_contrast(), *WRITER),
        (_topic_extract_response("sleeping pills"), *TOPIC),
        (_editor_response(REWRITTEN), *EDITOR),
        (verdict, verifier_model, VERIFIER[1]),
    ]
    return MockLLMProvider(
        [
            LLMResponse(content=c, model=m, usage=u, stop_reason="end_turn")
            for c, m, u in script
        ],
        cite_placeholders=True,
    )


async def _run(sqlite_store, llm, *, pricing, **config):
    pipeline = Pipeline(
        config=make_engine_config(**config),
        crawl_adapter=_make_adapter_with_markdown(
            "Sleeping pills can shorten sleep onset in acute insomnia, "
            "according to short-term randomised trials."
        ),
        evidence_store=sqlite_store,
        llm=llm,
        scorer=_scorer(),
        editor=_editor(llm),
        implied_claim_checker=_checker(llm),
        pricing=pricing,
    )
    return await pipeline.run(make_curation_request(), make_source_policy())


def _publish_metrics(result) -> dict:
    [rec] = [s for s in result.job.stages if s.stage == JobStage.PUBLISH]
    return rec.metrics


async def test_job_records_its_cost_priced_per_model(sqlite_store, caplog):
    llm = _llm()
    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await _run(sqlite_store, llm, pricing=load_model_pricing())

    assert len(llm.calls) == 4
    assert EXPECTED_USD == pytest.approx(0.0607)
    assert _publish_metrics(result)["cost_estimate_usd"] == pytest.approx(EXPECTED_USD)
    assert "est_cost=$0.0607" in caplog.text


async def test_implied_claim_calls_count_toward_the_job_totals(sqlite_store):
    """Audit 5.1: the checker's usage was discarded, so the totals, the budget
    and the completion line under-counted every topic-extraction call."""
    result = await _run(sqlite_store, _llm(), pricing=None)

    totals = _publish_metrics(result)["token_usage"]
    assert totals["input_tokens"] == 1000 + 100 + 500 + 200
    assert totals["output_tokens"] == 2000 + 50 + 1500 + 800

    [edit] = [s for s in result.job.stages if s.stage == JobStage.EDIT]
    assert edit.metrics["implied_claim_calls"] == 1
    assert edit.metrics["implied_claim_model"] == TOPIC[0]
    assert edit.metrics["implied_claim_tokens_input"] == 100
    assert edit.metrics["implied_claim_tokens_output"] == 50
    assert edit.metrics["model"] == EDITOR[0]

    # The per-stage records add up to the job totals: cost is derived from
    # the same calls the totals count.
    staged = sum(
        s.metrics.get(key, 0)
        for s in result.job.stages
        if s.stage in (JobStage.WRITE, JobStage.EDIT, JobStage.VERIFY)
        for key in ("tokens_input", "implied_claim_tokens_input")
    )
    assert staged == totals["input_tokens"]


NULL_CACHE = {"cache_creation_input_tokens": None, "cache_read_input_tokens": None}


@pytest.mark.parametrize("role", ["topic", "editor"])
async def test_a_null_cache_count_counts_as_zero(sqlite_store, role):
    """COR-07: a provider reporting a cache count as None (the SDK types
    them as Optional) on the implied-claim or editor reply failed the whole
    job with a TypeError at stage write. None now counts as 0."""
    from cce.models.job import JobStatus

    topic, editor = TOPIC[1], EDITOR[1]
    if role == "topic":
        topic = {**topic, **NULL_CACHE}
    else:
        editor = {**editor, **NULL_CACHE}
    script = [
        (_ai_flat_with_contrast(), *WRITER),
        (_topic_extract_response("sleeping pills"), TOPIC[0], topic),
        (_editor_response(REWRITTEN), EDITOR[0], editor),
        (_verifier_json(supported=10, total=10, gaps=0), *VERIFIER),
    ]
    llm = MockLLMProvider(
        [
            LLMResponse(content=c, model=m, usage=u, stop_reason="end_turn")
            for c, m, u in script
        ],
        cite_placeholders=True,
    )
    result = await _run(sqlite_store, llm, pricing=load_model_pricing())

    assert result.job.status == JobStatus.COMPLETED
    totals = _publish_metrics(result)["token_usage"]
    assert totals["input_tokens"] == 1000 + 100 + 500 + 200
    assert totals["cache_creation_input_tokens"] == 3000
    assert totals["cache_read_input_tokens"] == 3000
    [edit] = [s for s in result.job.stages if s.stage == JobStage.EDIT]
    assert edit.metrics["tokens_cache_write"] == 0
    assert edit.metrics["implied_claim_tokens_cache_write"] == 0
    assert _publish_metrics(result)["cost_estimate_usd"] == pytest.approx(EXPECTED_USD)


async def test_implied_claim_calls_count_toward_the_token_budget(sqlite_store):
    """Iteration 1 spends 6000 input+output tokens without the topic call and
    6150 with it; the gate fails, so the loop reaches the iteration-2
    checkpoint. A budget of 6100 stops the run there only if the topic call
    is counted (uncounted, the writer would be called again)."""
    from cce.models.job import JobStatus

    failing = _verifier_json(supported=3, total=10, unsupported=5, gaps=2)
    llm = _llm(verdict=failing)
    result = await _run(sqlite_store, llm, pricing=None, max_tokens_per_job=6100)

    assert len(llm.calls) == 4
    assert result.job.status == JobStatus.REVIEW_REQUIRED
    [stop] = [s for s in result.job.stages if (s.metrics or {}).get("budget_exceeded")]
    assert stop.metrics["tokens_spent"] == 6150


@pytest.mark.parametrize("pricing", [None, {}])
async def test_no_pricing_table_means_no_estimate(sqlite_store, pricing, caplog):
    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await _run(sqlite_store, _llm(), pricing=pricing)

    assert _publish_metrics(result)["cost_estimate_usd"] is None
    assert "est_cost" not in caplog.text


async def test_a_model_without_a_price_means_no_estimate(sqlite_store):
    """Never a partial sum: one unpriced model (an injected gateway's own
    ID, say) leaves the whole estimate out."""
    result = await _run(
        sqlite_store,
        _llm(verifier_model="my-gateway-model"),
        pricing=load_model_pricing(),
    )

    assert _publish_metrics(result)["cost_estimate_usd"] is None
