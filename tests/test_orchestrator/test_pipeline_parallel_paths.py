"""Integration tests for parallel per-path write-verify (audit P1, T-03.01).

Asserts the concurrency refactor speeds up multi-path runs, aggregates token
usage correctly across tasks, feeds the progress counter monotonically, and
attaches a `path` to each WRITE/VERIFY StageRecord.metrics entry so records
stay identifiable when they interleave.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import pytest

from cce.llm.base import LLMMessage, LLMResponse
from cce.models.job import JobStage
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import make_adapter, verifier_json, writer_json

pytestmark = pytest.mark.integration


_WRITE_LATENCY_S = 0.1
_VERIFY_LATENCY_S = 0.1


class _SleepyLLM:
    """LLM stub that sleeps per-call and dispatches on system-prompt content.

    Satisfies the LLMProvider protocol. Each call returns a writer- or
    verifier-shaped payload based on a keyword in the system prompt.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        kind = "verifier" if system and "fact-checking" in system else "writer"
        self.calls.append(kind)
        if kind == "writer":
            await asyncio.sleep(_WRITE_LATENCY_S)
            return LLMResponse(
                content=writer_json(),
                model="mock",
                stop_reason="end_turn",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 2,
                },
            )
        await asyncio.sleep(_VERIFY_LATENCY_S)
        return LLMResponse(
            content=verifier_json(supported=10, total=10, gaps=0),
            model="mock",
            stop_reason="end_turn",
            usage={
                "input_tokens": 20,
                "output_tokens": 3,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 4,
            },
        )


def _request_with_paths(*paths: str):
    return make_curation_request(paths=list(paths))


async def test_paths_run_concurrently(sqlite_store):
    config = make_engine_config()
    llm = _SleepyLLM()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    t_start = time.monotonic()
    result = await pipeline.run(
        _request_with_paths("blog", "summary", "faq"), make_source_policy()
    )
    wall_clock = time.monotonic() - t_start

    # Sequential would be 3 paths * (writer+verifier) * 0.1s = 0.6s.
    # Concurrent is bounded by max single-path = writer+verifier = 0.2s,
    # plus ~100ms of orchestration overhead (discovery, publish, etc).
    assert result.succeeded is True
    assert wall_clock < 0.5, (
        f"Expected wall-clock < 0.5s (parallel); got {wall_clock:.3f}s "
        f"(would be ~0.6s sequential)."
    )


async def test_token_usage_aggregated_across_paths(sqlite_store):
    config = make_engine_config()
    llm = _SleepyLLM()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    result = await pipeline.run(
        _request_with_paths("blog", "summary", "faq"), make_source_policy()
    )
    assert result.succeeded is True

    # Aggregate tokens live on the PUBLISH stage record.
    publish_records = [s for s in result.job.stages if s.stage == JobStage.PUBLISH]
    assert len(publish_records) == 1
    tu = publish_records[0].metrics["token_usage"]

    # Each path: 1 writer call + 1 verifier call at the pass-first-try stub.
    # Writer per call:  input=10, output=5,  cache_w=1, cache_r=2
    # Verifier per call:input=20, output=3,  cache_w=0, cache_r=4
    # 3 paths:          input=90, output=24, cache_w=3, cache_r=18
    assert tu == {
        "input_tokens": 90,
        "output_tokens": 24,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 18,
    }


async def test_progress_counter_increments_to_N(sqlite_store, caplog):
    config = make_engine_config()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_SleepyLLM(),
    )

    import logging

    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await pipeline.run(
            _request_with_paths("blog", "summary", "faq"), make_source_policy()
        )
    assert result.succeeded is True

    # The per-path "Progress: k/3 paths complete" line fires N times.
    progress_lines = [r for r in caplog.records if "paths complete" in r.getMessage()]
    assert len(progress_lines) == 3
    # Monotonic counter in the message bodies — must pass through 1, 2, 3.
    observed = [
        int(r.getMessage().split("/")[0].split(":")[-1].strip()) for r in progress_lines
    ]
    assert sorted(observed) == [1, 2, 3]


async def test_stage_records_carry_path(sqlite_store):
    config = make_engine_config()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_SleepyLLM(),
    )
    result = await pipeline.run(
        _request_with_paths("blog", "summary", "faq"), make_source_policy()
    )
    assert result.succeeded is True

    per_path_writes = Counter()
    per_path_verifies = Counter()
    for s in result.job.stages:
        if s.stage == JobStage.WRITE:
            per_path_writes[s.metrics["path"]] += 1
        elif s.stage == JobStage.VERIFY:
            per_path_verifies[s.metrics["path"]] += 1

    # PASS-on-first-iteration stub → exactly one write + one verify per path.
    assert dict(per_path_writes) == {"blog": 1, "summary": 1, "faq": 1}
    assert dict(per_path_verifies) == {"blog": 1, "summary": 1, "faq": 1}
