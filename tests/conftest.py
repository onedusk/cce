"""Shared test fixtures and factories for the CCE test suite."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import AsyncGenerator
from typing import Any, Callable

import pytest

from cce.config.types import (
    CrawlConfig,
    EngineConfig,
    EvidenceStoreConfig,
    LLMConfig,
    QualityGateConfig,
)
from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.base import LLMMessage, LLMProvider, LLMResponse
from cce.models.content import (
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence, SourceQuality
from cce.models.request import CurationRequest
from cce.policy.types import RecencyRule, ReputationRule, SourcePolicy
from cce.verification.verifier import VerificationReport


# ---------------------------------------------------------------------------
# Mock: LLMProvider
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """Protocol-compliant LLM mock with scripted responses and call recording.

    Satisfies: cce.llm.base.LLMProvider
    """

    def __init__(
        self,
        responses: list[LLMResponse | Callable[[], LLMResponse]] | None = None,
    ) -> None:
        self._responses: list[LLMResponse | Callable[[], LLMResponse]] = list(
            responses or []
        )
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "system": system,
            }
        )
        if not self._responses:
            raise RuntimeError("MockLLMProvider: no more scripted responses")
        resp = self._responses.pop(0)
        return resp() if callable(resp) else resp


# ---------------------------------------------------------------------------
# Mock: CrawlAdapter
# ---------------------------------------------------------------------------


class MockCrawlAdapter:
    """Protocol-compliant crawl mock with URL->result mapping.

    Satisfies: cce.discovery.adapters.base.CrawlAdapter
    """

    def __init__(
        self,
        url_map: dict[str, CrawlResult] | None = None,
        search_map: dict[str, list[str]] | None = None,
    ) -> None:
        self._url_map: dict[str, CrawlResult] = url_map or {}
        self._search_map: dict[str, list[str]] = search_map or {}

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        if request.url in self._url_map:
            return self._url_map[request.url]
        return CrawlResult(url=request.url, status_code=0)

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        return [await self.crawl(r) for r in requests]

    async def search(self, query: str, limit: int = 10) -> list[str]:
        if query in self._search_map:
            return self._search_map[query][:limit]
        raise NotImplementedError("MockCrawlAdapter: no search results configured")


# ---------------------------------------------------------------------------
# Factory: Evidence
# ---------------------------------------------------------------------------


def make_evidence(**overrides: Any) -> Evidence:
    """Build an Evidence object with sensible defaults.

    Any field can be overridden via keyword arguments.
    Auto-computes excerpt_hash from excerpt if not provided.
    """
    excerpt = overrides.get(
        "excerpt",
        "This is a test excerpt with enough content to pass the minimum length check.",
    )
    excerpt_hash = overrides.get(
        "excerpt_hash", hashlib.sha256(excerpt.encode()).hexdigest()
    )
    defaults: dict[str, Any] = {
        "id": f"ev_{uuid.uuid4().hex[:12]}",
        "url": "https://example.com/article",
        "title": "Test Article Title",
        "author": "Test Author",
        "published_at": datetime(2024, 1, 15, tzinfo=timezone.utc),
        "retrieved_at": datetime(2024, 3, 1, tzinfo=timezone.utc),
        "excerpt": excerpt,
        "excerpt_hash": excerpt_hash,
        "locator": "chunk:0",
        "source_quality": SourceQuality(
            is_peer_reviewed=False,
            is_primary_source=False,
            domain_reputation="unknown",
            conflict_of_interest=False,
        ),
    }
    defaults.update(overrides)
    return Evidence(**defaults)


# ---------------------------------------------------------------------------
# Factory: CurationRequest
# ---------------------------------------------------------------------------


def make_curation_request(**overrides: Any) -> CurationRequest:
    """Build a CurationRequest with sensible defaults."""
    defaults: dict[str, Any] = {
        "topic": "test topic",
        "subtopics": [],
        "paths": ["blog"],
        "audience": "general",
        "constraints": None,
        "policy_id": "test-policy",
        "risk_profile": "medium",
    }
    defaults.update(overrides)
    return CurationRequest(**defaults)


# ---------------------------------------------------------------------------
# Factory: SourcePolicy
# ---------------------------------------------------------------------------


def make_source_policy(**overrides: Any) -> SourcePolicy:
    """Build a SourcePolicy with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": "test-policy",
        "name": "Test Policy",
        "domains_allow": [],
        "domains_deny": [],
        "reputation": ReputationRule(
            require_peer_reviewed=False,
            require_primary_source=False,
            trusted_institutions=[],
            block_marketing=True,
        ),
        "recency": RecencyRule(max_age_days=None, prefer_recent=True),
        "max_sources_per_run": 50,
        "topic_overrides": [],
    }
    defaults.update(overrides)
    return SourcePolicy(**defaults)


# ---------------------------------------------------------------------------
# Factory: ContentUnit
# ---------------------------------------------------------------------------


def make_content_unit(**overrides: Any) -> ContentUnit:
    """Build a ContentUnit with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": f"cu_{uuid.uuid4().hex[:12]}",
        "path": "blog",
        "tags": [],
        "content": "Test content with a claim [ev:test_001].",
        "citations": [],
        "evidence_map": [],
        "scores": ContentScores(
            confidence=0.0,
            coverage=0.0,
            source_diversity=0.0,
        ),
        "lineage": ContentLineage(
            policy_id="test-policy",
            run_id="run-test",
            engine_version="0.1.0",
        ),
    }
    defaults.update(overrides)
    return ContentUnit(**defaults)


# ---------------------------------------------------------------------------
# Factory: CrawlResult
# ---------------------------------------------------------------------------


def make_crawl_result(**overrides: Any) -> CrawlResult:
    """Build a CrawlResult with sensible defaults."""
    defaults: dict[str, Any] = {
        "url": "https://example.com/article",
        "status_code": 200,
        "title": "Test Article",
        "author": "Test Author",
        "published_date": "2024-01-15T00:00:00Z",
        "markdown": (
            "# Test Article\n\n"
            "This is a substantial paragraph with enough content to pass "
            "the 50-character minimum check for evidence extraction.\n\n"
            "This is a second paragraph that also has sufficient length "
            "to be extracted as a separate evidence chunk."
        ),
        "raw_html": "",
        "metadata": {},
    }
    defaults.update(overrides)
    return CrawlResult(**defaults)


# ---------------------------------------------------------------------------
# Factory: VerificationReport
# ---------------------------------------------------------------------------


def make_verification_report(**overrides: Any) -> VerificationReport:
    """Build a VerificationReport with configurable claim counts."""
    defaults: dict[str, Any] = {
        "claims": [],
        "total_claims": 10,
        "supported": 8,
        "unsupported": 0,
        "uncited": 0,
        "leakage": 0,
        "conflicts": 0,
        "gaps_acknowledged": 2,
        "contradictions": [],
        "overall_feedback": "All claims verified.",
        "confidence_score": 0.9,
        "raw_response": "{}",
    }
    defaults.update(overrides)
    return VerificationReport(**defaults)


# ---------------------------------------------------------------------------
# Factory: Config objects
# ---------------------------------------------------------------------------


def make_engine_config(**overrides: Any) -> EngineConfig:
    """Build an EngineConfig with dummy API keys and test-friendly paths."""
    defaults: dict[str, Any] = {
        "llm": LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="test-api-key",
            temperature=0.2,
            max_tokens=4096,
        ),
        "evidence_store": EvidenceStoreConfig(
            backend="sqlite",
            sqlite_path=Path("test_evidence.db"),
        ),
        "crawl": CrawlConfig(
            adapter="firecrawl",
            api_key="test-crawl-key",
            rate_limit_rps=10.0,
            timeout_seconds=5,
        ),
        "quality_gate": {
            "medium": QualityGateConfig(
                autopublish_threshold=0.85,
                min_citations_per_paragraph=1,
                min_citation_density_ratio=0.9,
                max_writer_iterations=3,
            ),
        },
        "engine_version": "0.1.0-test",
    }
    defaults.update(overrides)
    return EngineConfig(**defaults)


def make_gate_config(**overrides: Any) -> QualityGateConfig:
    """Build a QualityGateConfig with explicit thresholds."""
    defaults: dict[str, Any] = {
        "autopublish_threshold": 0.85,
        "min_citations_per_paragraph": 1,
        "min_citation_density_ratio": 0.9,
        "max_writer_iterations": 3,
    }
    defaults.update(overrides)
    return QualityGateConfig(**defaults)


# ---------------------------------------------------------------------------
# Fixture: SQLiteEvidenceStore (real DB via tmp_path)
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_store(
    tmp_path: Path,
) -> AsyncGenerator[SQLiteEvidenceStore, None]:
    """Isolated SQLite evidence store per test.

    Creates a real database file in tmp_path, connects, yields,
    and closes after the test completes.
    """
    config = EvidenceStoreConfig(
        backend="sqlite",
        sqlite_path=tmp_path / "test_evidence.db",
    )
    store = SQLiteEvidenceStore(config)
    await store.connect()
    yield store
    await store.close()


# ---------------------------------------------------------------------------
# Fixture: MockLLMProvider convenience
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm() -> Callable[..., MockLLMProvider]:
    """Factory fixture that creates a MockLLMProvider from raw JSON strings.

    Usage:
        llm = mock_llm('{"content": "hello"}', '{"content": "world"}')
    """

    def _factory(*json_strings: str) -> MockLLMProvider:
        responses = [
            LLMResponse(content=s, model="mock", usage={}, stop_reason="end_turn")
            for s in json_strings
        ]
        return MockLLMProvider(responses=responses)

    return _factory
