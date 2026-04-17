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
    return MockLLMProvider(
        [
            LLMResponse(content=s, model="mock", stop_reason="end_turn")
            for s in json_strings
        ]
    )
