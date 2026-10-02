"""Shared helpers for pipeline orchestrator tests."""

import json

from cce.llm.base import LLMResponse
from tests.conftest import MockCrawlAdapter, MockLLMProvider, make_crawl_result


def writer_json(*, content: str = "Draft with citation [ev:ev_001].") -> str:
    return json.dumps(
        {
            "content": content,
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "Draft claim", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )


def claim_assessments(
    *,
    total: int,
    supported: int,
    unsupported: int,
    uncited: int,
    leakage: int,
    conflicts: int,
    gaps: int,
) -> list[str]:
    """One assessment per claim for these summary counts, padded to ``total``."""
    assessments = (
        ["supported"] * supported
        + ["unsupported"] * unsupported
        + ["uncited"] * uncited
        + ["leakage"] * leakage
        + ["conflict"] * conflicts
        + ["gap_acknowledged"] * gaps
    )
    if len(assessments) > total:
        raise ValueError(f"counts add up to {len(assessments)}, above total={total}")
    return assessments + ["unassessed"] * (total - len(assessments))


def verifier_json(
    *,
    supported: int = 8,
    total: int = 10,
    leakage: int = 0,
    conflicts: int = 0,
    unsupported: int = 0,
    uncited: int = 0,
    gaps: int = 2,
) -> str:
    """A verifier reply whose claim list carries the same counts as its
    summary: the verifier scores from the claims (COR-06). Claims up to
    ``total`` that no count covers get an assessment outside the vocabulary,
    which counts toward the total and in no bucket."""
    return json.dumps(
        {
            "claims": [
                {
                    "claim": f"Claim {i}",
                    "citation_ids": ["ev_001"],
                    "assessment": assessment,
                    "explanation": "OK",
                    "suggestion": "",
                }
                for i, assessment in enumerate(
                    claim_assessments(
                        total=total,
                        supported=supported,
                        unsupported=unsupported,
                        uncited=uncited,
                        leakage=leakage,
                        conflicts=conflicts,
                        gaps=gaps,
                    )
                )
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


def make_adapter():
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


def llm(*json_strings: str) -> MockLLMProvider:
    # Pipeline-level: scripted ev_001-style IDs stand for discovered evidence.
    return MockLLMProvider(
        [
            LLMResponse(content=s, model="mock", stop_reason="end_turn")
            for s in json_strings
        ],
        cite_placeholders=True,
    )
