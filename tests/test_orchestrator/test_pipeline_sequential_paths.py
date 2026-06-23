"""Integration tests for sequential-with-awareness per-path write-verify (M03).

Replaces the old parallel-paths suite. Asserts the M03 refactor: paths run in
``request.paths`` order, each later path's writer receives a non-empty sibling
digest (LEARN/first path gets none), token usage still aggregates across paths,
the progress counter advances 1..N, and every WRITE/VERIFY StageRecord carries
its path. A failing path now aborts the run directly (no TaskGroup cancellation)
and later paths never execute.
"""

from __future__ import annotations

import logging
import re
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


_SIBLING_MARKER = "ALREADY COVERED BY SIBLING ARTICLES"


class _RecordingLLM:
    """LLM stub that records each writer call and always returns PASS.

    Dispatches writer vs verifier on the system prompt (verifier prompts mention
    'fact-checking'). For each writer call it captures the output path (from the
    'Output path:' prompt line) and whether a sibling-context block was injected,
    so tests can assert ordering + digest threading. Per-call usage matches the
    legacy parallel suite so the token-aggregation assertion is unchanged.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        # (path, has_sibling_block, sibling_text)
        self.writer_calls: list[tuple[str, bool, str]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        text = messages[0].content
        if system and "fact-checking" in system:
            self.calls.append("verifier")
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
        match = re.search(r"Output path: (.+)", text)
        assert match is not None, "writer prompt missing 'Output path:' line"
        path = match.group(1).strip()
        self.calls.append("writer")
        self.writer_calls.append((path, _SIBLING_MARKER in text, text))
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


def _request_with_paths(*paths: str):
    return make_curation_request(paths=list(paths))


async def test_paths_run_in_request_order(sqlite_store):
    llm = _RecordingLLM()
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    result = await pipeline.run(
        _request_with_paths("learn", "explore", "apply"), make_source_policy()
    )

    assert result.succeeded is True
    # Writer calls fire in request.paths order under sequential execution.
    assert [p for p, _, _ in llm.writer_calls] == ["learn", "explore", "apply"]


async def test_later_paths_receive_sibling_digest(sqlite_store):
    llm = _RecordingLLM()
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    result = await pipeline.run(
        _request_with_paths("learn", "explore", "apply"), make_source_policy()
    )
    assert result.succeeded is True

    has_sibling = {p: flag for p, flag, _ in llm.writer_calls}
    # First path sees no siblings; later paths do.
    assert has_sibling["learn"] is False
    assert has_sibling["explore"] is True
    assert has_sibling["apply"] is True

    # The digest names the prior path and carries its claim text (non-empty).
    explore_prompt = next(t for p, _, t in llm.writer_calls if p == "explore")
    assert "From the 'learn' article" in explore_prompt
    assert "Draft claim" in explore_prompt
    # Prose-level only: the block tells the writer it may still cite shared
    # sources (citation invariant preserved — ADR-003).
    assert "not on citations" in explore_prompt

    # By the third path, the digest has accumulated both prior siblings.
    apply_prompt = next(t for p, _, t in llm.writer_calls if p == "apply")
    assert "From the 'learn' article" in apply_prompt
    assert "From the 'explore' article" in apply_prompt


async def test_token_usage_aggregated_across_paths(sqlite_store):
    llm = _RecordingLLM()
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    result = await pipeline.run(
        _request_with_paths("blog", "summary", "faq"), make_source_policy()
    )
    assert result.succeeded is True

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
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_RecordingLLM(),
    )

    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await pipeline.run(
            _request_with_paths("blog", "summary", "faq"), make_source_policy()
        )
    assert result.succeeded is True

    progress_lines = [r for r in caplog.records if "paths complete" in r.getMessage()]
    assert len(progress_lines) == 3
    # Sequential execution passes through 1, 2, 3 in order.
    observed = [
        int(r.getMessage().split("/")[0].split(":")[-1].strip()) for r in progress_lines
    ]
    assert observed == [1, 2, 3]


async def test_stage_records_carry_path(sqlite_store):
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_RecordingLLM(),
    )
    result = await pipeline.run(
        _request_with_paths("blog", "summary", "faq"), make_source_policy()
    )
    assert result.succeeded is True

    per_path_writes: Counter[str] = Counter()
    per_path_verifies: Counter[str] = Counter()
    for s in result.job.stages:
        if s.stage == JobStage.WRITE:
            per_path_writes[s.metrics["path"]] += 1
        elif s.stage == JobStage.VERIFY:
            per_path_verifies[s.metrics["path"]] += 1

    # PASS-on-first-iteration stub → exactly one write + one verify per path.
    assert dict(per_path_writes) == {"blog": 1, "summary": 1, "faq": 1}
    assert dict(per_path_verifies) == {"blog": 1, "summary": 1, "faq": 1}


class _RaisingLLM:
    """Raises on the first writer call; records every call before raising.

    Used to pin sequential abort semantics: a failing path raises directly (no
    TaskGroup), the run is marked FAILED, and later paths never execute.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        text = messages[0].content
        if system and "fact-checking" in system:
            self.calls.append(("verifier", ""))
            return LLMResponse(
                content=verifier_json(supported=10, total=10, gaps=0),
                model="mock",
                stop_reason="end_turn",
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        match = re.search(r"Output path: (.+)", text)
        path = match.group(1).strip() if match else "?"
        self.calls.append(("writer", path))
        raise RuntimeError(f"stub raised on writer for '{path}'")


async def test_failing_path_aborts_run_and_skips_later_paths(sqlite_store):
    llm = _RaisingLLM()
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )

    result = await pipeline.run(
        _request_with_paths("learn", "explore", "apply"), make_source_policy()
    )

    assert result.failed is True
    assert result.job.status.value == "failed"
    assert result.job.error is not None
    assert "stub raised" in result.job.error.message

    # Sequential abort: only the first path's writer ran; no verifier and no
    # later-path writers were ever invoked.
    assert llm.calls == [("writer", "learn")]
