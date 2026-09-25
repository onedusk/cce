"""The package carries per-path verification results (B7).

A reviewer spot-checks instead of rereading: per requested path, the
terminal gate decision and its feedback, the verifier's per-claim verdicts,
and the writer's declared gaps.
"""

from __future__ import annotations

import json

import pytest

from cce.models.job import JobStatus
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import llm as _llm
from tests.test_orchestrator.conftest import make_adapter, verifier_json

pytestmark = pytest.mark.integration


def writer_reply(
    content: str = "Draft [ev:ev_001].", gaps: list[str] | None = None
) -> str:
    return json.dumps(
        {
            "content": content,
            "citations_used": ["ev_001"] if content else [],
            "evidence_map": (
                [{"claim": "Draft claim", "evidence_ids": ["ev_001"]}]
                if content
                else []
            ),
            "gaps": gaps or [],
        }
    )


def uncited_report() -> str:
    """Verifier reply with explicit claims: 3 supported, 2 uncited."""
    claims = [
        {
            "claim": f"Supported claim {i}",
            "citation_ids": ["ev_001"],
            "assessment": "supported",
            "explanation": "The excerpt says this.",
            "suggestion": "",
        }
        for i in range(3)
    ] + [
        {
            "claim": f"Uncited claim {i}",
            "citation_ids": [],
            "assessment": "uncited",
            "explanation": f"No citation for figure {i}.",
            "suggestion": "Cite the excerpt that gives it.",
        }
        for i in range(2)
    ]
    return json.dumps(
        {
            "claims": claims,
            "summary": {
                "total_claims": 5,
                "supported": 3,
                "unsupported": 0,
                "uncited": 2,
                "leakage": 0,
                "conflicts": 0,
                "gaps_acknowledged": 0,
            },
            "overall_feedback": "Two figures lack citations.",
            "contradictions": [],
        }
    )


def _pipeline(store, llm, **config) -> Pipeline:
    return Pipeline(
        config=make_engine_config(**config),
        crawl_adapter=make_adapter(),
        evidence_store=store,
        llm=llm,
    )


async def test_review_package_carries_verdicts_feedback_and_gaps(sqlite_store):
    """B7 acceptance: a REVIEW job's package holds per-claim verdicts and the
    gate's reasons (medium profile: 3 iterations, then review)."""
    script = []
    for _ in range(3):
        script += [
            writer_reply(gaps=["No dosage data in the evidence"]),
            uncited_report(),
        ]
    result = await _pipeline(sqlite_store, _llm(*script)).run(
        make_curation_request(), make_source_policy()
    )

    assert result.job.status == JobStatus.REVIEW_REQUIRED
    [record] = result.package.verification
    assert record.path == "blog"
    assert record.decision == "review"
    assert record.iteration == 3
    assert record.unit_id == result.package.units[0].id
    assert "factual claim(s) have no citations" in record.feedback
    assert "Max iterations (3) reached" in record.feedback
    assert record.writer_gaps == ["No dosage data in the evidence"]
    assert record.report is not None
    uncited = [c for c in record.report.claims if c.assessment == "uncited"]
    assert [c.explanation for c in uncited] == [
        "No citation for figure 0.",
        "No citation for figure 1.",
    ]
    assert record.report.uncited == 2
    assert record.report.overall_feedback == "Two figures lack citations."


async def test_pass_package_links_the_record_to_its_unit(sqlite_store):
    result = await _pipeline(
        sqlite_store,
        _llm(writer_reply(), verifier_json(supported=10, total=10, gaps=0)),
    ).run(make_curation_request(), make_source_policy())

    [record] = result.package.verification
    assert record.decision == "pass"
    assert record.iteration == 1
    assert record.unit_id == result.package.units[0].id
    assert record.report is not None and record.report.supported == 10


async def test_path_without_a_unit_is_still_recorded(sqlite_store):
    """An empty draft produces no unit and no gate result: the record says
    so (terminal fail, no report) and keeps the writer's gaps."""
    result = await _pipeline(
        sqlite_store, _llm(writer_reply(content="", gaps=["Nothing on this topic"]))
    ).run(make_curation_request(), make_source_policy())

    [record] = result.package.verification
    assert record.unit_id is None
    assert record.decision == "fail"
    assert record.iteration is None
    assert record.report is None
    assert record.writer_gaps == ["Nothing on this topic"]


async def test_gaps_belong_to_the_write_that_produced_the_unit(sqlite_store):
    """Iteration 1 writes (gaps A) and fails the gate; iteration 2's writer
    returns nothing (gaps B). The kept unit is iteration 1's, so are its gaps."""
    script = [
        writer_reply(gaps=["A"]),
        uncited_report(),
        writer_reply(content="", gaps=["B"]),
    ]
    result = await _pipeline(sqlite_store, _llm(*script)).run(
        make_curation_request(), make_source_policy()
    )

    [record] = result.package.verification
    assert record.writer_gaps == ["A"]
    assert record.iteration == 1


async def test_budget_note_is_captured_in_the_persisted_feedback(sqlite_store):
    from tests.test_orchestrator.test_pipeline_budget import (
        ONE_ITERATION_TOKENS,
        VERIFIER_USAGE,
        WRITER_USAGE,
        _llm_with_usage,
    )

    llm = _llm_with_usage(
        (writer_reply(), WRITER_USAGE),
        (verifier_json(supported=3, total=10, unsupported=5, gaps=2), VERIFIER_USAGE),
    )
    result = await _pipeline(
        sqlite_store, llm, max_tokens_per_job=ONE_ITERATION_TOKENS
    ).run(make_curation_request(), make_source_policy())

    [record] = result.package.verification
    assert "Token budget exceeded" in record.feedback


async def test_wrongly_typed_claim_fields_are_coerced_not_fatal(sqlite_store):
    """A report that passes the shape check but carries odd field types (a
    provider ignoring the schema) must not fail the job — a strict model's
    ValidationError would quote the reply into the job record."""
    odd = json.loads(verifier_json(supported=10, total=10, gaps=0))
    odd["claims"] = [
        {
            "claim": 5,
            "citation_ids": "ev_001",
            "assessment": 7,
            "explanation": None,
            "suggestion": ["x"],
        }
    ]
    result = await _pipeline(sqlite_store, _llm(writer_reply(), json.dumps(odd))).run(
        make_curation_request(), make_source_policy()
    )

    assert result.job.status != JobStatus.FAILED
    assert result.job.error is None
    [claim] = result.package.verification[0].report.claims
    assert claim.claim == "5"
    assert claim.citation_ids == []
    assert claim.assessment == "7"
    assert claim.explanation == "None"
