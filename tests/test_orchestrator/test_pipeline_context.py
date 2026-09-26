"""Pinned context through the pipeline (B11).

Context skips discovery and every cap, is shown to the writer and verifier
as settled context before the sources, is stored under the caller's IDs, and
is citable and checked like any evidence. Context IDs here are not 12-hex,
so ``cite_placeholders`` never rewrites them (ev_001 stays "the first
discovered excerpt").
"""

from __future__ import annotations

import json

import pytest

from cce.models.job import JobStage, JobStatus
from cce.models.paths import PathConfig
from cce.orchestrator.pipeline import Pipeline
from cce.output.mdx.citations import build_citation_index
from tests.conftest import (
    MockCrawlAdapter,
    make_curation_request,
    make_engine_config,
    make_evidence,
    make_source_policy,
)
from tests.test_orchestrator.conftest import llm as _llm
from tests.test_orchestrator.conftest import make_adapter

pytestmark = pytest.mark.integration

PAGE_URL = "https://example.com/article"
PAGE_TEXT = (
    "This is a substantial paragraph with real content that exceeds "
    "the fifty character minimum for evidence extraction in tests."
)  # make_adapter()'s single chunk


def _context():
    return [
        make_evidence(id="ctx_a", url="consumer://acme/canon", excerpt="No em-dashes."),
        make_evidence(
            id="ctx_b", url="consumer://acme/canon", excerpt="Plans cost $12."
        ),
        make_evidence(id="ctx_c", url="consumer://acme/canon", excerpt="Est. 2019."),
    ]


def _writer(content: str, cited: list[str]) -> str:
    return json.dumps(
        {
            "content": content,
            "citations_used": cited,
            "evidence_map": [{"claim": "A claim", "evidence_ids": cited}],
            "gaps": [],
        }
    )


def _verifier_all_supported(ids: list[str]) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim": f"Claim citing {i}",
                    "citation_ids": [i],
                    "assessment": "supported",
                    "explanation": "The entry states it.",
                    "suggestion": "",
                }
                for i in ids
            ],
            "summary": {
                "total_claims": len(ids),
                "supported": len(ids),
                "unsupported": 0,
                "uncited": 0,
                "leakage": 0,
                "conflicts": 0,
                "gaps_acknowledged": 0,
            },
            "overall_feedback": "All supported.",
            "contradictions": [],
        }
    )


def _pipeline(store, llm, adapter=None) -> Pipeline:
    return Pipeline(
        config=make_engine_config(),
        crawl_adapter=adapter or make_adapter(),
        evidence_store=store,
        llm=llm,
        # max_evidence=1 would drop short, undated context if it were capped.
        path_configs={"blog": PathConfig(id="blog", name="Blog", max_evidence=1)},
    )


def _discover_metrics(result) -> dict:
    [rec] = [s for s in result.job.stages if s.stage == JobStage.DISCOVER]
    return rec.metrics or {}


async def test_context_is_cited_beside_a_source_and_passes(sqlite_store):
    """B11 accept (mock half): a paragraph cites a short context statement
    next to a discovered source; the gate resolves both and passes."""
    llm = _llm(
        _writer(
            "## Pricing\n\nPlans cost $12 [ev:ctx_b], as the page confirms [ev:ev_001].",
            ["ctx_b", "ev_001"],
        ),
        _verifier_all_supported(["ctx_b", "ev_001"]),
    )
    request = make_curation_request(context=_context())
    result = await _pipeline(sqlite_store, llm).run(request, make_source_policy())

    assert result.job.status == JobStatus.COMPLETED
    for call in llm.calls:  # writer, then verifier
        prompt = call["messages"][0].content
        context_part, sources_part = prompt.split("=== SOURCES ===")
        assert "=== CONTEXT (settled) ===" in context_part
        for ev_id in ("ctx_a", "ctx_b", "ctx_c"):
            assert f'<evidence id="{ev_id}">' in context_part
        assert PAGE_TEXT in sources_part

    package = result.package
    assert package is not None
    assert [ev.id for ev in package.evidence][:3] == ["ctx_a", "ctx_b", "ctx_c"]
    assert len(package.evidence) == 4
    [unit] = package.units
    assert {c.evidence_id for c in unit.citations} >= {"ctx_b"}
    index = build_citation_index(unit.content, {e.id: e for e in package.evidence})
    assert "[^?]" not in index.content
    # Carried on the package, never written to the evidence store.
    assert await sqlite_store.get("ctx_b") is None
    assert _discover_metrics(result)["context_duplicates"] == 0


async def test_context_only_run_proceeds_when_discovery_finds_nothing(sqlite_store):
    llm = _llm(
        _writer("## Canon\n\nNo em-dashes [ev:ctx_a].", ["ctx_a"]),
        _verifier_all_supported(["ctx_a"]),
    )
    result = await _pipeline(
        sqlite_store, llm, adapter=MockCrawlAdapter(search_map={}, url_map={})
    ).run(make_curation_request(context=_context()), make_source_policy())

    assert result.job.status == JobStatus.COMPLETED
    assert [ev.id for ev in result.package.evidence] == ["ctx_a", "ctx_b", "ctx_c"]


async def test_discovered_copy_of_a_pinned_excerpt_is_dropped(sqlite_store):
    """Context pins the page's own text at its URL: the discovered chunk is
    the same excerpt at the same URL and is dropped from the run, so the
    writer sees it once, under CONTEXT. The crawl is still stored, under its
    own ID."""
    pinned = make_evidence(id="ctx_page", url=PAGE_URL, excerpt=PAGE_TEXT)
    llm = _llm(
        _writer("## Page\n\nA claim [ev:ctx_page].", ["ctx_page"]),
        _verifier_all_supported(["ctx_page"]),
    )
    result = await _pipeline(sqlite_store, llm).run(
        make_curation_request(context=[pinned]), make_source_policy()
    )

    assert result.job.status == JobStatus.COMPLETED
    assert [ev.id for ev in result.package.evidence] == ["ctx_page"]
    assert _discover_metrics(result)["context_duplicates"] == 1
    assert llm.calls[0]["messages"][0].content.count(PAGE_TEXT) == 1
    assert await sqlite_store.get("ctx_page") is None
    assert await sqlite_store.count() == 1


async def test_context_never_reaches_a_later_run_as_a_crawl(sqlite_store):
    """Final review of B11: stored context came back to the next run (another
    caller, no context) as the already-crawled content of its URL, so the
    page was never fetched and the pinned text was cited as that source."""
    secret = make_evidence(
        id="ctx_secret", url=PAGE_URL, excerpt="CONFIDENTIAL tenant note: 30% off."
    )
    first = await _pipeline(
        sqlite_store,
        _llm(
            _writer("## Note\n\nA claim [ev:ctx_secret].", ["ctx_secret"]),
            _verifier_all_supported(["ctx_secret"]),
        ),
        adapter=MockCrawlAdapter(search_map={}, url_map={}),
    ).run(make_curation_request(context=[secret]), make_source_policy())
    assert first.job.status == JobStatus.COMPLETED

    from tests.test_orchestrator.conftest import verifier_json
    from tests.test_orchestrator.test_pipeline_verification_records import (
        writer_reply,
    )

    llm = _llm(writer_reply(), verifier_json(supported=10, total=10, gaps=0))
    second = await _pipeline(sqlite_store, llm).run(
        make_curation_request(), make_source_policy()
    )

    prompt = llm.calls[0]["messages"][0].content
    assert "CONFIDENTIAL" not in prompt
    assert PAGE_TEXT in prompt  # the page was crawled
    assert "ctx_secret" not in {ev.id for ev in second.package.evidence}


async def test_runs_without_context_carry_no_context_metric(sqlite_store):
    from tests.test_orchestrator.conftest import verifier_json
    from tests.test_orchestrator.test_pipeline_verification_records import (
        writer_reply,
    )

    result = await _pipeline(
        sqlite_store,
        _llm(writer_reply(), verifier_json(supported=10, total=10, gaps=0)),
    ).run(make_curation_request(), make_source_policy())

    assert "context_duplicates" not in _discover_metrics(result)


@pytest.mark.unit
async def test_writer_called_directly_formats_and_keeps_context_citations():
    from cce.llm.base import LLMResponse
    from cce.synthesis.writer import Writer
    from tests.conftest import MockLLMProvider

    reply = _writer("Plans cost $12 [ev:ctx_b].", ["ctx_b"])
    llm = MockLLMProvider([LLMResponse(content=reply, model="m")])
    source = make_evidence(excerpt="A discovered excerpt about the plan pricing.")
    out = await Writer(llm).write(
        make_curation_request(context=_context()), [source], "blog"
    )

    prompt = llm.calls[0]["messages"][0].content
    assert prompt.index("=== CONTEXT (settled) ===") < prompt.index("=== SOURCES ===")
    assert "You have 4 evidence excerpts" in prompt
    assert [c.evidence_id for c in out.unit.citations] == ["ctx_b"]
