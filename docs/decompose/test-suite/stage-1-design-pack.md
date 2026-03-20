# Stage 1: Design Pack — test-suite

> Comprehensive test suite for the CCE Phase 1 pipeline. Covers ~147 tests across 11 test files targeting all 8 packages.

---

## Assumptions & Constraints *(required)*

- Tests target the existing Phase 1 codebase only — no tests for unbuilt Phases 2–4
- No real API calls (Anthropic, Firecrawl) in the default test run — external services are mocked at the protocol level
- All source modules use constructor-based DI with `typing.Protocol`, making mock injection straightforward
- All I/O is async — tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- SQLite tests use real `aiosqlite` via `tmp_path` (no mocking the database)
- Python ≥ 3.11, managed with uv, no additional test dependencies beyond `pytest` + `pytest-asyncio` (already in dev deps)

---

## Target Platform & Tooling Baseline *(required)*

| Component | Version | Reference |
|-----------|---------|-----------|
| Python | ≥ 3.11 | pyproject.toml |
| pytest | ≥ 8.0 | pyproject.toml `[project.optional-dependencies]` |
| pytest-asyncio | ≥ 0.24 | pyproject.toml `[project.optional-dependencies]` |
| ruff | ≥ 0.8 | pyproject.toml `[project.optional-dependencies]` |
| aiosqlite | ≥ 0.20 | pyproject.toml `[project.dependencies]` — used in SQLite integration tests |
| pydantic | ≥ 2.0 | pyproject.toml `[project.dependencies]` — model validation in model tests |

No new dependencies required. Everything needed is already installed.

---

## Data Model / Schema *(required)*

The test suite's "data model" is the set of shared fixtures and factories in `conftest.py`. These produce the domain objects that flow through every test.

### Entity: MockLLMProvider

**Purpose:** Protocol-compliant mock that returns scripted LLM responses and records all calls for assertion.

**Fields:**

| Field | Type | Nullable | Purpose |
|-------|------|:--------:|---------|
| `responses` | `list[LLMResponse]` | No | Queue of responses, popped in order on each `.complete()` call |
| `calls` | `list[dict]` | No | Record of every call: messages, system, temperature, max_tokens |

**Satisfies:** `LLMProvider` protocol (`llm/base.py`)

### Entity: MockCrawlAdapter

**Purpose:** Protocol-compliant mock that maps URLs to crawl results and queries to URL lists.

**Fields:**

| Field | Type | Nullable | Purpose |
|-------|------|:--------:|---------|
| `url_map` | `dict[str, CrawlResult]` | No | URL → CrawlResult for `crawl()` / `crawl_many()` |
| `search_map` | `dict[str, list[str]]` | No | Query string → list of URLs for `search()` |

**Satisfies:** `CrawlAdapter` protocol (`discovery/adapters/base.py`)

### Entity: Factory Functions

| Factory | Returns | Key Defaults |
|---------|---------|-------------|
| `make_evidence(**overrides)` | `Evidence` | `id="ev_test_001"`, `url="https://example.com/article"`, `excerpt="Test excerpt..."`, auto-computed `excerpt_hash` |
| `make_curation_request(**overrides)` | `CurationRequest` | `topic="test topic"`, `paths=["blog"]`, `risk_profile="medium"` |
| `make_source_policy(**overrides)` | `SourcePolicy` | `id="test-policy"`, empty allow/deny, reasonable reputation/recency |
| `make_content_unit(**overrides)` | `ContentUnit` | Zero scores, default lineage, `content="Test content"` |
| `make_crawl_result(**overrides)` | `CrawlResult` | `status_code=200`, `markdown="Test content..."`, `url="https://example.com"` |
| `make_verification_report(**overrides)` | `VerificationReport` | Configurable claim counts, `confidence_score=0.9` |

---

## Architecture *(required)*

### Component Diagram

```
tests/
├── conftest.py ─────────────────── Shared fixtures (factories, mocks, tmp stores)
│
├── Unit tests (no I/O) ────────── Import source modules directly
│   ├── test_parsing.py              parsing.py
│   ├── test_models/                 models/*
│   ├── test_discovery/ (static)     discoverer.py static methods
│   └── test_verification/gate       gate.py
│
├── Mock-LLM tests ─────────────── Use MockLLMProvider
│   ├── test_synthesis/              writer.py
│   └── test_verification/verifier   verifier.py
│
├── Integration tests ──────────── Real SQLite + real filesystem
│   ├── test_evidence/               sqlite.py (tmp_path DB)
│   ├── test_config/                 loader.py (tmp_path YAML)
│   ├── test_policy/                 loader.py (tmp_path YAML)
│   ├── test_discovery/ (async)      discoverer.py with MockCrawlAdapter
│   ├── test_orchestrator/           pipeline.py (all mocks + real SQLite)
│   └── test_output.py              output.py (tmp_path files)
```

### Architectural Pattern

**Mirror layout** — test directory structure mirrors `src/cce/` package structure. Each source module gets one test file in the corresponding test directory.

### Concurrency / Threading Model

All tests run in a single-threaded async event loop via `asyncio_mode = "auto"`. No threads, no multiprocessing. SQLite tests use `aiosqlite` (async wrapper over SQLite's blocking C API).

---

## UI/UX Layout *(skip — library/CLI project)*

N/A — this is a test suite, not a user-facing application.

---

## Features *(required)*

### Core — Test Infrastructure

- [ ] `conftest.py` with all shared fixtures: MockLLMProvider, MockCrawlAdapter, model factories, sqlite_store fixture, config fixtures
- [ ] Pytest markers defined in `pyproject.toml`: unit, integration, slow, e2e
- [ ] All test directories created with `__init__.py` files

### Core — Unit Tests (~100 tests)

- [ ] `test_parsing.py` — 14 tests for `extract_json()` and `_repair_json()`
- [ ] `test_discovery/test_discoverer.py` (unit) — 28 tests for static methods: `_build_queries`, `_passes_policy`, `_resolve_overrides`, `_chunk_content`, heuristics (`_looks_peer_reviewed`, `_looks_primary`, `_assess_reputation`, `_looks_marketing`), `_extract_evidence`
- [ ] `test_verification/test_gate.py` — 20 tests for `QualityGate.evaluate()`, `_check_citation_density()`, `_has_fixable_issues()`, `GateResult` properties
- [ ] `test_verification/test_verifier.py` — 12 unit tests for `VerificationReport.pass_rate`, `_parse_response()`, confidence calculation, `_format_evidence()`
- [ ] `test_synthesis/test_writer.py` — 10 unit tests for `_build_evidence_block()`, `_parse_response()`, `WriterOutput` properties
- [ ] `test_models/test_models.py` — 5 tests for frozen constraints, score bounds, enum values, request defaults
- [ ] `test_output.py` (unit) — 5 tests for `serialize_result()`, datetime/enum/Path serialization
- [ ] `test_config/test_loader.py` — 8 tests for `load_config()`, env var fallbacks, type coercion

### Core — Integration Tests (~47 tests)

- [ ] `test_discovery/test_discoverer.py` (integration) — 5 tests for `discover()` with MockCrawlAdapter
- [ ] `test_evidence/test_sqlite.py` — 19 tests for SQLiteEvidenceStore CRUD, dedup, search, serialization roundtrip
- [ ] `test_synthesis/test_writer.py` (integration) — 3 tests for `write()` with MockLLMProvider (prompt structure, feedback, temperature)
- [ ] `test_verification/test_verifier.py` (integration) — 1 test for `verify()` prompt structure
- [ ] `test_orchestrator/test_pipeline.py` — 8 tests for full pipeline: happy path, no evidence, rewrite loop, review, multi-path, exception handling
- [ ] `test_policy/test_loader.py` — 6 tests for YAML parsing, directory loading
- [ ] `test_output.py` (integration) — 3 tests for `write_output()` file creation
- [ ] `test_config/test_loader.py` — included in unit count above (uses `tmp_path` + `monkeypatch`)

### Quality of Life

- [ ] CI excludes `e2e` marker by default: `uv run pytest -m "not e2e"`
- [ ] Fast feedback path: `uv run pytest -m unit` runs ~100 tests in <5s

---

## Integration Points *(if applicable)*

### Anthropic API (mocked)

- **API surface:** `LLMProvider.complete()` — only method used by Writer and Verifier
- **Mock approach:** `MockLLMProvider` returns scripted `LLMResponse` objects
- **No real API calls** in unit/integration tests

### Firecrawl API (mocked)

- **API surface:** `CrawlAdapter.crawl()`, `.crawl_many()`, `.search()` — used by Discoverer
- **Mock approach:** `MockCrawlAdapter` maps URLs to `CrawlResult` objects
- **No real API calls** in unit/integration tests

### SQLite (real, isolated)

- **API surface:** `SQLiteEvidenceStore` — 7 async methods
- **Real database** via `aiosqlite` + `tmp_path` — each test gets its own DB file
- **No mocking** — real SQL execution validates schema, indexes, constraints

---

## Security & Privacy Plan *(required)*

- **Data at rest:** Tests use `tmp_path` (auto-cleaned by pytest). No persistent data created.
- **Data in transit:** No network calls in unit/integration tests. All external services mocked.
- **Permissions required:** Filesystem read/write to `tmp_path` only.
- **System exposure:** No secrets in test code. API keys are dummy strings (`"test-key"`). `.env` file is not read by tests.
- **Optional hardening:** N/A

---

## Architecture Decision Records *(required, minimum 3)*

### ADR-001 — Mock at Protocol Level, Not SDK Level

- **Status:** Accepted
- **Context:** Writer and Verifier depend on `LLMProvider`, which is implemented by `AnthropicProvider`. We could mock at the Anthropic SDK level (patching `anthropic.AsyncAnthropic`) or at the protocol level (implementing `LLMProvider` with scripted responses).
- **Decision:** Mock at the protocol level with `MockLLMProvider`.
- **Consequences:** Tests are provider-agnostic — they work regardless of LLM backend. Tests exercise the actual JSON parsing logic in Writer/Verifier. Downside: `AnthropicProvider` itself is not tested (acceptable — it's a thin SDK wrapper; e2e tests will cover it later).

### ADR-002 — Real SQLite via tmp_path, Not Mocked

- **Status:** Accepted
- **Context:** `SQLiteEvidenceStore` uses `aiosqlite` for all persistence. We could mock the database or use a real SQLite instance.
- **Decision:** Use real SQLite via pytest's `tmp_path` fixture. Each test gets an isolated `.db` file.
- **Consequences:** Tests validate actual SQL (schema, indexes, UNIQUE constraints, WAL mode). Serialization roundtrips are tested end-to-end. Tests are slightly slower than mocks (~ms per test, negligible). No false confidence from SQL mocks that don't match real behavior.

### ADR-003 — Factory Functions Over Pytest Fixtures for Model Construction

- **Status:** Accepted
- **Context:** Tests need `Evidence`, `CurationRequest`, `SourcePolicy`, etc. with varying field values. We could use pytest fixtures (parameterized or not) or factory functions with `**overrides`.
- **Decision:** Use factory functions (`make_evidence(**overrides)`) that return new objects on each call. Register a few as pytest fixtures for the most common cases.
- **Consequences:** Maximum flexibility — each test customizes exactly the fields it cares about. No fixture dependency chains. Factories are plain functions, easy to understand. All models are frozen Pydantic BaseModels, so factories can't accidentally share mutable state.

### ADR-004 — Mirror Source Layout for Test Directory Structure

- **Status:** Accepted
- **Context:** Tests could be organized by type (all unit tests together, all integration tests together) or by source module (mirroring `src/cce/`).
- **Decision:** Mirror `src/cce/` — `test_discovery/test_discoverer.py` tests `src/cce/discovery/discoverer.py`, etc.
- **Consequences:** Easy to find tests for a given module. `uv run pytest tests/test_discovery/` runs all discovery tests. Co-locates unit and integration tests for the same module in one file (separated by comments/sections). Trade-off: some test files mix unit and integration markers.

### ADR-005 — No Additional Test Dependencies

- **Status:** Accepted
- **Context:** We could add `pytest-mock`, `factory-boy`, `hypothesis`, `coverage`, or other test utilities.
- **Decision:** Use only `pytest` + `pytest-asyncio` (already in dev deps). Write mocks by hand.
- **Consequences:** Zero new dependencies to install or maintain. Hand-written mocks for 2 protocols (LLMProvider, CrawlAdapter) are ~30 lines each — simpler than learning a mocking framework. `coverage` can be added later when we want metrics. `hypothesis` is valuable but out of scope for initial suite.

---

## Product Decision Records *(required, minimum 2)*

### PDR-001 — Prioritize Pipeline Logic Over Adapter Testing

- **Status:** Accepted
- **Problem:** With 0% coverage and limited time, we need to sequence test development for maximum risk reduction.
- **Decision:** Test pipeline logic first (parsing → discoverer statics → gate → verifier → writer), then storage, then config/policy, then orchestrator integration last.
- **Rationale:** Pipeline logic bugs produce wrong output (bad citations, incorrect gate decisions, hallucinated content). These are the highest-risk, hardest-to-detect failures. Adapter bugs (Firecrawl, Anthropic) are caught by e2e tests and are simpler (thin wrappers). Testing from leaves to root ensures each integration test can trust its dependencies.

### PDR-002 — Start With conftest.py Before Any Test Files

- **Status:** Accepted
- **Problem:** Every test file needs Evidence objects, mock LLMs, mock crawl adapters, and config objects. Without shared fixtures, each file would duplicate this setup.
- **Decision:** Build `conftest.py` with all factories and mocks first, before writing any test file.
- **Rationale:** One-time investment that makes every subsequent test file faster to write. Consistent test data across the suite. Changes to model constructors require updates in one place.

### PDR-003 — Defer E2E Tests

- **Status:** Accepted
- **Problem:** E2E tests (real Anthropic + Firecrawl calls) are expensive ($), slow, and non-deterministic.
- **Decision:** Define the `e2e` marker now but write no e2e tests in this phase. Unit + integration tests cover all logic paths.
- **Rationale:** The protocol-level mock strategy means Writer/Verifier parsing and Discoverer extraction logic are fully tested without API calls. E2E tests add value for smoke testing but are better suited for a CI nightly job after the core suite is stable.

---

## Condensed PRD *(required)*

**Goal:** Build a comprehensive test suite for CCE Phase 1 that covers all pipeline logic, storage, configuration, and orchestration with ~147 tests.

**Primary User Stories:**

1. As a developer, I can run `uv run pytest` and get fast feedback on whether my changes broke any pipeline behavior.
2. As a developer, I can run `uv run pytest -m unit` for <5s feedback on pure logic changes.
3. As a developer, I can trust that the quality gate, writer, verifier, and discoverer logic is correct because each decision path has a test.

**Non-Goals (this version):**

- E2E tests with real Anthropic/Firecrawl APIs
- Test coverage metrics or coverage gates
- Performance/load testing
- Tests for unbuilt Phases 2–4 (tagging, REST API, platform integration)
- Property-based testing (hypothesis)

**Success Criteria:**

- All 147 tests pass on `uv run pytest`
- `uv run pytest -m unit` completes in <5 seconds
- Every public method in the pipeline has at least one test
- Zero test interdependencies — any test can run in isolation

---

## Data Lifecycle & Retention *(required)*

- **Deletion behavior:** All test data is created in `tmp_path` (pytest-managed temp directories) and automatically cleaned up after each test session.
- **Export format:** N/A — no persistent test data.
- **Retention policy:** No test artifacts are retained. CI logs capture test output.

---

## Testing Strategy *(required)*

This *is* the testing strategy — see Features section above and the detailed test plan at `docs/internal/tests/test-plan.md` for per-test breakdowns.

### Unit Tests (~100)

- JSON extraction: direct parse, code fences, bracket matching, repair, edge cases
- Discoverer statics: query building, policy filtering, content chunking, quality heuristics
- Quality gate: 3-way decision logic, citation density, fixable issues, feedback generation
- Verifier: pass rate calculation, confidence scoring with penalties, response parsing
- Writer: evidence block formatting, response parsing, diversity calculation
- Models: frozen constraints, score bounds, enum values, defaults
- Serialization: datetime, enum, Path conversion

### Integration Tests (~47)

- SQLiteEvidenceStore: full CRUD, dedup, search, serialization roundtrip (real SQLite)
- Discoverer.discover(): full flow with MockCrawlAdapter
- Pipeline.run(): happy path, error paths, write-verify loop, multi-path (mocked externals + real SQLite)
- Config/policy loading: YAML files + env var precedence (real filesystem)
- Output: file creation and content validation (real filesystem)

---

## Implementation Plan *(required)*

1. **M01 — Test Infrastructure** — Create `conftest.py` with all factories and mocks, add pytest markers to `pyproject.toml`, create test directory structure with `__init__.py` files
2. **M02 — Parsing & Models** — Write `test_parsing.py` (14 tests) and `test_models/test_models.py` (5 tests) — foundation utilities with zero dependencies
3. **M03 — Discovery Unit Tests** — Write unit tests for `test_discovery/test_discoverer.py` (28 tests) — static methods covering query building, policy filtering, chunking, heuristics
4. **M04 — Quality Gate** — Write `test_verification/test_gate.py` (20 tests) — decision logic, citation density, feedback generation
5. **M05 — Verifier & Writer** — Write `test_verification/test_verifier.py` (13 tests) and `test_synthesis/test_writer.py` (13 tests) — LLM response parsing with MockLLMProvider
6. **M06 — Evidence Storage** — Write `test_evidence/test_sqlite.py` (19 tests) — SQLiteEvidenceStore CRUD, dedup, search, serialization
7. **M07 — Config & Policy** — Write `test_config/test_loader.py` (8 tests) and `test_policy/test_loader.py` (6 tests) — YAML parsing, env var precedence
8. **M08 — Pipeline & Output Integration** — Write discovery integration tests (5 tests), `test_orchestrator/test_pipeline.py` (8 tests), and `test_output.py` (8 tests) — full pipeline flows with all mocks wired together

---

## Before Moving On

Verify before proceeding to Stage 2:

- [x] Every assumption is written down
- [x] Platform/tooling versions are specific and researched
- [x] Data model covers every entity with all fields, types, and relationships
- [x] Architecture pattern is named and justified
- [x] At least 3 ADRs are written (5 total)
- [x] At least 2 PDRs are written (3 total)
- [x] Implementation plan has an ordered milestone list (8 milestones)
- [x] Project described in one sentence: "Comprehensive test suite for CCE Phase 1 covering all pipeline logic with ~147 tests"
