# Stage 2: Implementation Skeletons — test-suite

> Compilable Python code for all test infrastructure. Copy-paste starting points for implementation.

---

## Data Model Code

### File: `tests/conftest.py`

```python
"""Shared test fixtures and factories for the CCE test suite."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
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
    Citation,
    ClaimMapping,
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence, SourceQuality
from cce.models.request import CurationRequest
from cce.policy.types import RecencyRule, ReputationRule, SourcePolicy
from cce.verification.verifier import (
    ClaimVerification,
    Contradiction,
    VerificationReport,
)


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
    """Protocol-compliant crawl mock with URL→result mapping.

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
    excerpt = overrides.get("excerpt", "This is a test excerpt with enough content to pass the minimum length check.")
    defaults: dict[str, Any] = {
        "id": f"ev_{uuid.uuid4().hex[:12]}",
        "url": "https://example.com/article",
        "title": "Test Article Title",
        "author": "Test Author",
        "published_at": datetime(2024, 1, 15, tzinfo=timezone.utc),
        "retrieved_at": datetime(2024, 3, 1, tzinfo=timezone.utc),
        "excerpt": excerpt,
        "excerpt_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
        "locator": "chunk:0",
        "source_quality": SourceQuality(
            is_peer_reviewed=False,
            is_primary_source=False,
            domain_reputation="unknown",
            conflict_of_interest=False,
        ),
    }
    # Recompute hash if excerpt was overridden but hash was not
    if "excerpt" in overrides and "excerpt_hash" not in overrides:
        defaults["excerpt_hash"] = hashlib.sha256(
            overrides["excerpt"].encode()
        ).hexdigest()
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
async def sqlite_store(tmp_path: Path) -> SQLiteEvidenceStore:
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
    yield store  # type: ignore[misc]
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
```

---

## Interface Contracts

The test suite does not expose an API. The "contracts" it must satisfy are the three `typing.Protocol` interfaces that the mocks implement. These are defined in the source code and summarized here for reference.

### Protocol: `LLMProvider` (`src/cce/llm/base.py`)

```python
@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse: ...
```

**Mock implementation:** `MockLLMProvider` (in conftest.py)
- Pops from `self._responses` queue
- Records call args to `self.calls`

### Protocol: `CrawlAdapter` (`src/cce/discovery/adapters/base.py`)

```python
@runtime_checkable
class CrawlAdapter(Protocol):
    async def crawl(self, request: CrawlRequest) -> CrawlResult: ...
    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]: ...
    async def search(self, query: str, limit: int = 10) -> list[str]: ...
```

**Mock implementation:** `MockCrawlAdapter` (in conftest.py)
- `crawl()`: looks up `request.url` in `self._url_map`, returns `CrawlResult(status_code=0)` on miss
- `crawl_many()`: delegates to `crawl()` for each request
- `search()`: looks up query in `self._search_map`, raises `NotImplementedError` on miss

### Protocol: `EvidenceStore` (`src/cce/evidence/store.py`)

```python
@runtime_checkable
class EvidenceStore(Protocol):
    async def put(self, evidence: Evidence) -> bool: ...
    async def put_many(self, evidence: list[Evidence]) -> int: ...
    async def get(self, evidence_id: str) -> Evidence | None: ...
    async def get_many(self, evidence_ids: list[str]) -> list[Evidence]: ...
    async def search(self, *, url: str | None = None, topic: str | None = None, limit: int = 50) -> list[Evidence]: ...
    async def exists_by_hash(self, excerpt_hash: str) -> bool: ...
    async def count(self) -> int: ...
```

**Test approach:** Use `SQLiteEvidenceStore` directly via `sqlite_store` fixture (real DB, not mocked).

---

## Pytest Configuration Changes

### File: `pyproject.toml` (MODIFY — add markers)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "unit: Fast tests with no I/O or external dependencies",
    "integration: Tests using real I/O (SQLite, filesystem) with mocked external services",
    "slow: Tests that take >1s (multi-iteration pipeline loops)",
    "e2e: End-to-end tests requiring real ANTHROPIC_API_KEY and FIRECRAWL_API_KEY",
]
```

---

## Test Directory Structure (CREATE)

```
tests/
├── __init__.py                    (exists)
├── conftest.py                    (CREATE)
├── test_parsing.py                (CREATE)
├── test_output.py                 (CREATE)
├── test_models/
│   ├── __init__.py                (CREATE)
│   └── test_models.py             (CREATE)
├── test_config/
│   ├── __init__.py                (CREATE)
│   └── test_loader.py             (CREATE)
├── test_policy/
│   ├── __init__.py                (CREATE)
│   └── test_loader.py             (CREATE)
├── test_discovery/
│   ├── __init__.py                (CREATE)
│   └── test_discoverer.py         (CREATE)
├── test_evidence/
│   ├── __init__.py                (CREATE)
│   └── test_sqlite.py             (CREATE)
├── test_synthesis/
│   ├── __init__.py                (CREATE)
│   └── test_writer.py             (CREATE)
├── test_verification/
│   ├── __init__.py                (CREATE)
│   ├── test_verifier.py           (CREATE)
│   └── test_gate.py               (CREATE)
└── test_orchestrator/
    ├── __init__.py                (CREATE)
    └── test_pipeline.py           (CREATE)
```

---

## Documentation Artifacts

### Data Model Reference

#### MockLLMProvider

**Key:** N/A (instantiated per test)

**Fields:** `responses` (list[LLMResponse]), `calls` (list[dict])

**Satisfies:** `LLMProvider` protocol

**Operations:**
- `complete()` — pops next response from queue, records call args
- Raises `RuntimeError` if response queue is empty

#### MockCrawlAdapter

**Key:** N/A (instantiated per test)

**Fields:** `url_map` (dict[str, CrawlResult]), `search_map` (dict[str, list[str]])

**Satisfies:** `CrawlAdapter` protocol

**Operations:**
- `crawl()` — returns mapped result or `CrawlResult(status_code=0)`
- `crawl_many()` — delegates to `crawl()` per request
- `search()` — returns mapped URLs or raises `NotImplementedError`

#### Factory Functions

| Function | Returns | Override Pattern | Auto-computation |
|----------|---------|-----------------|------------------|
| `make_evidence(**kw)` | `Evidence` | Any field via kwargs | `excerpt_hash` = SHA-256 of `excerpt` |
| `make_curation_request(**kw)` | `CurationRequest` | Any field via kwargs | None |
| `make_source_policy(**kw)` | `SourcePolicy` | Any field via kwargs | None |
| `make_content_unit(**kw)` | `ContentUnit` | Any field via kwargs | `id` = random UUID prefix |
| `make_crawl_result(**kw)` | `CrawlResult` | Any field via kwargs | None |
| `make_verification_report(**kw)` | `VerificationReport` | Any field via kwargs | None |
| `make_engine_config(**kw)` | `EngineConfig` | Any field via kwargs | None |
| `make_gate_config(**kw)` | `QualityGateConfig` | Any field via kwargs | None |

#### Fixtures

| Fixture | Scope | Yields | Cleanup |
|---------|-------|--------|---------|
| `sqlite_store` | function | `SQLiteEvidenceStore` (connected) | `await store.close()` |
| `mock_llm` | function | Callable factory → `MockLLMProvider` | None |

### Example Payloads

**Example: MockLLMProvider for Writer test**

```python
# Writer returns JSON with content, citations, evidence_map, gaps
writer_response = '{"content": "AI models rely on training data [ev:ev_001].", "citations_used": ["ev_001"], "evidence_map": [{"claim": "AI models rely on training data", "evidence_ids": ["ev_001"]}], "gaps": []}'

llm = MockLLMProvider(responses=[
    LLMResponse(content=writer_response, model="mock", usage={}, stop_reason="end_turn"),
])
```

**Example: MockLLMProvider for Verifier test**

```python
# Verifier returns JSON with claims, summary, contradictions, overall_feedback
verifier_response = '''{
  "claims": [
    {"claim": "AI models rely on training data", "citation_ids": ["ev_001"], "assessment": "supported", "explanation": "Directly stated in evidence", "suggestion": ""}
  ],
  "summary": {"total_claims": 1, "supported": 1, "unsupported": 0, "uncited": 0, "leakage": 0, "conflicts": 0, "gaps_acknowledged": 0},
  "overall_feedback": "All claims verified.",
  "contradictions": []
}'''

llm = MockLLMProvider(responses=[
    LLMResponse(content=verifier_response, model="mock", usage={}, stop_reason="end_turn"),
])
```

**Example: MockCrawlAdapter for Discovery test**

```python
adapter = MockCrawlAdapter(
    search_map={
        "test topic": ["https://example.com/article", "https://example.org/study"],
    },
    url_map={
        "https://example.com/article": CrawlResult(
            url="https://example.com/article",
            status_code=200,
            title="Test Article",
            author="Author A",
            published_date="2024-01-15T00:00:00Z",
            markdown="This is a substantial paragraph about the topic.\n\nSecond paragraph with more details.",
        ),
        "https://example.org/study": CrawlResult(
            url="https://example.org/study",
            status_code=200,
            title="Academic Study",
            author="Researcher B",
            published_date="2024-02-01T00:00:00Z",
            markdown="Study findings show significant results in the field.",
        ),
    },
)
```

**Example: make_evidence factory**

```python
# Default evidence
ev = make_evidence()

# Customized evidence
ev = make_evidence(
    id="ev_custom",
    url="https://nih.gov/paper",
    excerpt="Peer-reviewed finding about gene therapy.",
    source_quality=SourceQuality(
        is_peer_reviewed=True,
        is_primary_source=True,
        domain_reputation="trusted",
    ),
)
```

**Example: Pipeline integration test setup**

```python
# Wire all mocks together for a pipeline test
config = make_engine_config()
adapter = MockCrawlAdapter(
    search_map={"test topic": ["https://example.com/a"]},
    url_map={"https://example.com/a": make_crawl_result()},
)
store = sqlite_store  # from fixture (real SQLite)
llm = MockLLMProvider(responses=[
    LLMResponse(content=writer_json, ...),   # writer call
    LLMResponse(content=verifier_json, ...),  # verifier call
])
pipeline = Pipeline(config=config, crawl_adapter=adapter, evidence_store=store, llm=llm)
result = await pipeline.run(make_curation_request(), make_source_policy())
```

---

## Before Moving On

Verify before proceeding to Stage 3:

- [x] Every entity from Stage 1's data model has corresponding code (`MockLLMProvider`, `MockCrawlAdapter`, 8 factory functions, 2 fixtures)
- [x] All factory functions produce valid instances of their target types
- [x] Mock classes satisfy their respective protocols (`LLMProvider`, `CrawlAdapter`)
- [x] Helper functions for common operations included (auto-computed `excerpt_hash`, UUID-based IDs)
- [x] Code parses without errors in Python 3.11+
- [x] Interface contracts cover all 3 protocols referenced in Stage 1
- [x] Documentation artifacts accurately reflect the code
- [x] Non-obvious decisions have inline comments (response queue exhaustion, hash auto-computation)
