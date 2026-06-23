"""Tests for cce.orchestrator.pipeline — full pipeline orchestration."""

import json
import re

import pytest

from cce.llm.base import LLMMessage, LLMResponse
from cce.models.job import Job, JobStatus
from cce.orchestrator.pipeline import Pipeline, _per_path_iteration_counts
from cce.output.mdx.citations import build_citation_index
from cce.verification.gate import GateDecision
from tests.conftest import (
    MockCrawlAdapter,
    make_curation_request,
    make_engine_config,
    make_evidence,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    llm as _llm,
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

# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pipeline_happy_path(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    llm = _llm(_writer_json(), _verifier_json())

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
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

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.failed is True
    assert result.job.status == JobStatus.FAILED


@pytest.mark.integration
async def test_pipeline_single_pass(sqlite_store):
    config = make_engine_config()
    adapter = _make_adapter()
    # High confidence → PASS on first iteration
    llm = _llm(_writer_json(), _verifier_json(supported=10, total=10, gaps=0))

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
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

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.succeeded is True
    assert len(result.gate_results) >= 2
    # Second writer call (index 2) should have received feedback
    assert len(llm.calls) == 4  # writer1, verifier1, writer2, verifier2
    second_writer_prompt = llm.calls[2]["messages"][0].content
    assert "VERIFIER FEEDBACK" in second_writer_prompt


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

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
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

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
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

    pipeline = Pipeline(
        config=config, crawl_adapter=adapter, evidence_store=sqlite_store, llm=llm
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.failed is True
    assert result.job.error is not None
    assert "Network error" in result.job.error.message


# ---------------------------------------------------------------------------
# Claims-path invariants on a fixture trio (T-03.01)
#
# Pins gate attribution + per-path iteration counts + citation resolution
# BEFORE the M03 concurrent→sequential refactor, so they form a regression
# baseline. The dispatch LLM routes each call by inspecting the prompt (writer
# path from the "Output path:" line, verifier path from a PATHTAG_ marker in
# the embedded draft) instead of a shared response queue, so the same tests are
# valid whether paths run concurrently (pre-M03) or sequentially (post-M03).
# ---------------------------------------------------------------------------


_TRIO = ["learn", "explore", "apply"]


def _trio_writer_json(path: str, *, empty: bool = False) -> str:
    """Writer payload carrying a path-identifying PATHTAG marker + one citation.

    ``empty=True`` yields ``content=""`` so the path's writer reports
    ``has_content=False`` and drops out of the loop before any gate evaluation
    (terminal FAIL — review F-2).
    """
    content = "" if empty else f"PATHTAG_{path} Draft body [ev:ev_001]."
    return json.dumps(
        {
            "content": content,
            "citations_used": [] if empty else ["ev_001"],
            "evidence_map": []
            if empty
            else [{"claim": "c", "evidence_ids": ["ev_001"]}],
            "gaps": ["nothing"] if empty else [],
        }
    )


class _PathDispatchLLM:
    """Order-independent LLM stub for the trio invariant tests.

    Routes by prompt content rather than a shared pop queue:
      - verifier calls (system mentions 'fact-checking') consume the next
        scripted verdict FOR THAT PATH (matched via the PATHTAG_ marker the
        writer embedded in the draft);
      - writer calls return that path's scripted payload (path read from the
        'Output path: X' prompt line).

    Per-path verifier state is keyed by path, so interleaving across paths
    (concurrent execution) cannot scramble a single path's FAIL→PASS sequence.
    """

    def __init__(
        self,
        *,
        writer_by_path: dict[str, str],
        verifier_by_path: dict[str, list[str]],
    ) -> None:
        self._writer_by_path = writer_by_path
        self._verifier_by_path = {p: list(v) for p, v in verifier_by_path.items()}
        self.calls: list[tuple[str, str]] = []

    def _verifier_path(self, text: str) -> str:
        for p in self._writer_by_path:
            if f"PATHTAG_{p}" in text:
                return p
        raise AssertionError(f"no PATHTAG marker in verifier prompt: {text[:120]!r}")

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        text = messages[0].content
        usage = {"input_tokens": 1, "output_tokens": 1}
        if system and "fact-checking" in system:
            path = self._verifier_path(text)
            self.calls.append(("verifier", path))
            payload = self._verifier_by_path[path].pop(0)
            return LLMResponse(
                content=payload, model="mock", stop_reason="end_turn", usage=usage
            )
        match = re.search(r"Output path: (.+)", text)
        assert match is not None, "writer prompt missing 'Output path:' line"
        path = match.group(1).strip()
        self.calls.append(("writer", path))
        return LLMResponse(
            content=self._writer_by_path[path],
            model="mock",
            stop_reason="end_turn",
            usage=usage,
        )


@pytest.mark.integration
async def test_trio_gate_attribution_completes_and_citations_resolve(sqlite_store):
    """All-PASS trio: COMPLETED, per-path counts [1,1,1], every marker resolves."""
    llm = _PathDispatchLLM(
        writer_by_path={p: _trio_writer_json(p) for p in _TRIO},
        verifier_by_path={
            p: [_verifier_json(supported=10, total=10, gaps=0)] for p in _TRIO
        },
    )
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=_make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )
    result = await pipeline.run(
        make_curation_request(paths=_TRIO), make_source_policy()
    )

    assert result.job.status == JobStatus.COMPLETED
    assert result.package is not None
    assert {u.path for u in result.package.units} == set(_TRIO)

    # Per-path terminal attribution: every path reached exactly one iteration.
    assert _per_path_iteration_counts(result.job, _TRIO) == [1, 1, 1]

    # Citation invariant: each unit's [ev:ID] markers resolve to a footnote —
    # no orphaned [^?] — when scanned against the cited evidence id.
    ev_lookup = {"ev_001": make_evidence(id="ev_001")}
    for unit in result.package.units:
        cited = build_citation_index(unit.content, ev_lookup)
        assert "[^?]" not in cited.content
        assert len(cited.citations) == 1
        assert "[^1]" in cited.content


@pytest.mark.integration
async def test_trio_empty_content_path_routes_to_review(sqlite_store):
    """An empty-content path contributes zero gate results → terminal FAIL,
    so the job routes to REVIEW_REQUIRED and yields no unit for that path."""
    llm = _PathDispatchLLM(
        writer_by_path={
            "learn": _trio_writer_json("learn"),
            "explore": _trio_writer_json("explore", empty=True),
            "apply": _trio_writer_json("apply"),
        },
        verifier_by_path={
            "learn": [_verifier_json(supported=10, total=10, gaps=0)],
            "apply": [_verifier_json(supported=10, total=10, gaps=0)],
        },
    )
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=_make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )
    result = await pipeline.run(
        make_curation_request(paths=_TRIO), make_source_policy()
    )

    # Empty path produced nothing publishable; the other two passed.
    assert {u.path for u in result.package.units} == {"learn", "apply"}
    assert result.job.status == JobStatus.REVIEW_REQUIRED
    # Per-path counts attribute the empty path to zero iterations (no WRITE
    # record is appended when the writer returns no content).
    assert _per_path_iteration_counts(result.job, _TRIO) == [1, 0, 1]
    # The empty path never reached the verifier.
    assert ("verifier", "explore") not in llm.calls


@pytest.mark.integration
async def test_trio_rewrite_path_iteration_counts(sqlite_store):
    """One path FAILs then PASSes; per-path counts attribute the extra
    iteration to that path alone (gate routing unchanged)."""
    llm = _PathDispatchLLM(
        writer_by_path={p: _trio_writer_json(p) for p in _TRIO},
        verifier_by_path={
            "learn": [
                _verifier_json(supported=3, total=10, unsupported=5, gaps=2),
                _verifier_json(supported=10, total=10, gaps=0),
            ],
            "explore": [_verifier_json(supported=10, total=10, gaps=0)],
            "apply": [_verifier_json(supported=10, total=10, gaps=0)],
        },
    )
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=_make_adapter(),
        evidence_store=sqlite_store,
        llm=llm,
    )
    result = await pipeline.run(
        make_curation_request(paths=_TRIO), make_source_policy()
    )

    assert result.job.status == JobStatus.COMPLETED
    assert _per_path_iteration_counts(result.job, _TRIO) == [2, 1, 1]


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
