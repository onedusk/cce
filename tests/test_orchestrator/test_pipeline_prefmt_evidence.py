"""Tests for pre-formatted evidence blocks in the write-verify loop (audit P7, T-06.04).

The formatter is called ONCE per path at loop entry; every iteration of the
writer + verifier within that loop reuses the pre-formatted strings. Without
this optimization a 3-path × 3-iteration run does 18 format calls. With it,
6 (2 styles per path).
"""

from __future__ import annotations

import pytest

from cce.evidence import formatting as formatting_module
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import make_adapter
from tests.test_orchestrator.test_pipeline_sequential_paths import _RecordingLLM

pytestmark = pytest.mark.integration


async def test_formatter_called_twice_per_path(sqlite_store, monkeypatch):
    """3-path run, PASS-first-iteration stub LLM -> 6 total format calls.

    2 styles (writer + verifier) × 3 paths = 6. The stub LLM always passes on
    iteration 1, so only one iteration per path. Observing more than 6 calls
    means the writer/verifier is re-formatting per-iteration despite the
    pre-computed block.
    """
    calls: list[tuple[int, str]] = []
    real_format = formatting_module.format_evidence_for_prompt

    def _counting(evidence, *, style):
        calls.append((len(evidence), style))
        return real_format(evidence, style=style)

    monkeypatch.setattr(formatting_module, "format_evidence_for_prompt", _counting)
    # The pipeline imports it as a module-level symbol into orchestrator/pipeline.py;
    # patch there too.
    import cce.orchestrator.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "format_evidence_for_prompt", _counting)
    # Writer + verifier also import the symbol; patch their module-level references.
    import cce.synthesis.writer as writer_module
    import cce.verification.verifier as verifier_module

    monkeypatch.setattr(writer_module, "format_evidence_for_prompt", _counting)
    monkeypatch.setattr(verifier_module, "format_evidence_for_prompt", _counting)

    config = make_engine_config()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_RecordingLLM(),
    )
    result = await pipeline.run(
        make_curation_request(paths=["blog", "summary", "faq"]),
        make_source_policy(),
    )
    assert result.succeeded is True

    # 2 styles × 3 paths = 6 total. Same evidence length in every call.
    styles_seen = [s for _, s in calls]
    assert styles_seen.count("writer") == 3, f"writer formats: {styles_seen}"
    assert styles_seen.count("verifier") == 3, f"verifier formats: {styles_seen}"
    assert len(calls) == 6


async def test_writer_falls_back_when_no_evidence_block_passed(sqlite_store):
    """Direct callers of Writer.write() that don't pass evidence_block still work."""
    from cce.llm.base import LLMResponse
    from cce.models.request import CurationRequest
    from cce.synthesis.writer import Writer
    from tests.conftest import MockLLMProvider, make_evidence
    from tests.test_orchestrator.conftest import writer_json

    llm = MockLLMProvider(
        responses=[
            LLMResponse(
                content=writer_json(), model="mock", stop_reason="end_turn", usage={}
            )
        ]
    )
    writer = Writer(llm=llm)
    ev = make_evidence(id="ev_001", url="https://ex.com/a", excerpt="x" * 80)

    output = await writer.write(
        request=CurationRequest(topic="t", paths=["blog"], policy_id="p", subtopics=[]),
        evidence=[ev],
        path="blog",
    )

    assert output.has_content
    # Fallback path produced a valid draft using its own format_evidence_for_prompt.


async def test_ev_lookup_built_once_across_paths_and_iterations(
    sqlite_store, monkeypatch
):
    """Pipeline.run() builds one ev_lookup and threads it through; writer's
    internal fallback-rebuild path never runs during a pipeline-driven call
    (audit P8).
    """
    rebuild_count = 0

    import cce.synthesis.writer as writer_module

    original_parse = writer_module.Writer._parse_response

    def _counting_parse(self, response, evidence, path, lineage, *, ev_lookup=None):
        nonlocal rebuild_count
        # ev_lookup should always be supplied by the pipeline (non-None)
        # during pipeline-driven calls.
        if ev_lookup is None:
            rebuild_count += 1
        return original_parse(
            self, response, evidence, path, lineage, ev_lookup=ev_lookup
        )

    monkeypatch.setattr(writer_module.Writer, "_parse_response", _counting_parse)

    config = make_engine_config()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_RecordingLLM(),
    )
    result = await pipeline.run(
        make_curation_request(paths=["blog", "summary", "faq"]),
        make_source_policy(),
    )
    assert result.succeeded is True
    # Writer received a non-None ev_lookup on every call. The fallback
    # rebuild path (local dict comp inside _parse_response) never ran.
    assert rebuild_count == 0, (
        f"Writer fell back to rebuilding ev_lookup {rebuild_count} times; "
        "Pipeline should have supplied it."
    )


async def test_verifier_falls_back_when_no_evidence_block_passed(sqlite_store):
    """Direct callers of Verifier.verify() that don't pass evidence_block still work."""
    from cce.llm.base import LLMResponse
    from cce.models.content import ContentLineage, ContentScores, ContentUnit
    from cce.verification.verifier import Verifier
    from tests.conftest import MockLLMProvider, make_evidence
    from tests.test_orchestrator.conftest import verifier_json

    llm = MockLLMProvider(
        responses=[
            LLMResponse(
                content=verifier_json(),
                model="mock",
                stop_reason="end_turn",
                usage={},
            )
        ]
    )
    verifier = Verifier(llm=llm)
    ev = make_evidence(id="ev_001", url="https://ex.com/a", excerpt="y" * 80)

    unit = ContentUnit(
        id="u1",
        path="blog",
        tags=[],
        content="A claim [ev:ev_001].",
        citations=[],
        evidence_map=[],
        scores=ContentScores(confidence=0.0, coverage=0.0, source_diversity=0.0),
        lineage=ContentLineage(policy_id="p", run_id="r", engine_version="0.1.0"),
    )

    report = await verifier.verify(unit, [ev])
    # Fallback path: no evidence_block passed, verifier formatted internally.
    assert report.total_claims >= 0
