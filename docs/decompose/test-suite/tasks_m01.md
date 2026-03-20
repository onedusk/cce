# Stage 4: Task Specifications — Milestone 01: Test Infrastructure

> Create the shared test foundation: pytest markers, directory structure, conftest.py with all mocks and factories.
>
> Fulfills: ADR-001 (protocol-level mocks), ADR-002 (sqlite_store fixture), ADR-003 (factory functions), ADR-004 (mirror layout), ADR-005 (no new deps), PDR-002 (conftest first), PDR-003 (e2e marker defined)

---

- [ ] **T-01.01 — Add pytest markers to pyproject.toml**
  - **File:** `pyproject.toml` (MODIFY)
  - **Depends on:** None
  - **Outline:**
    - Add `markers` list to existing `[tool.pytest.ini_options]` section
    - Define 4 markers: `unit`, `integration`, `slow`, `e2e`
    - Keep existing `testpaths` and `asyncio_mode` settings unchanged
  - **Acceptance:**
    - `uv run pytest --markers` lists all 4 custom markers
    - `uv run pytest --co -m unit` runs without error (even with 0 tests)
    - Existing `testpaths = ["tests"]` and `asyncio_mode = "auto"` are preserved

---

- [ ] **T-01.02 — Create test directory structure**
  - **File:** `tests/test_models/__init__.py`, `tests/test_config/__init__.py`, `tests/test_policy/__init__.py`, `tests/test_discovery/__init__.py`, `tests/test_evidence/__init__.py`, `tests/test_synthesis/__init__.py`, `tests/test_verification/__init__.py`, `tests/test_orchestrator/__init__.py` (CREATE)
  - **Depends on:** None
  - **Outline:**
    - Create 8 subdirectories under `tests/`
    - Each directory gets an empty `__init__.py`
    - Existing `tests/__init__.py` is untouched
  - **Acceptance:**
    - All 8 directories exist with `__init__.py` files
    - `uv run pytest --co` runs without import errors
    - No changes to `tests/__init__.py`

---

- [ ] **T-01.03 — Create conftest.py with mocks and factories**
  - **File:** `tests/conftest.py` (CREATE)
  - **Depends on:** T-01.01, T-01.02
  - **Outline:**
    - Copy the `conftest.py` skeleton from Stage 2 (`docs/decompose/test-suite/stage-2-implementation-skeletons.md`)
    - Implement `MockLLMProvider` class:
      - Constructor takes `responses: list[LLMResponse | Callable]`
      - `async complete()` pops from response queue, records call to `self.calls`
      - Raises `RuntimeError` when queue is empty
    - Implement `MockCrawlAdapter` class:
      - Constructor takes `url_map: dict[str, CrawlResult]`, `search_map: dict[str, list[str]]`
      - `crawl()` returns mapped result or `CrawlResult(status_code=0)`
      - `crawl_many()` delegates to `crawl()` per request
      - `search()` returns mapped URLs or raises `NotImplementedError`
    - Implement 8 factory functions:
      - `make_evidence(**overrides)` — auto-computes `excerpt_hash` from `excerpt`
      - `make_curation_request(**overrides)` — defaults: `topic="test topic"`, `paths=["blog"]`, `risk_profile="medium"`
      - `make_source_policy(**overrides)` — defaults: empty allow/deny, `max_sources_per_run=50`
      - `make_content_unit(**overrides)` — defaults: zero scores, default lineage
      - `make_crawl_result(**overrides)` — defaults: `status_code=200`, multi-paragraph markdown
      - `make_verification_report(**overrides)` — defaults: 10 claims, 8 supported, 2 gaps
      - `make_engine_config(**overrides)` — defaults: dummy API keys, medium gate config
      - `make_gate_config(**overrides)` — defaults: threshold=0.85, max_iters=3
    - Implement 2 pytest fixtures:
      - `sqlite_store` (async) — creates `SQLiteEvidenceStore` with `tmp_path`, connects, yields, closes
      - `mock_llm` — factory fixture returning `MockLLMProvider` from raw JSON strings
  - **Acceptance:**
    - `from tests.conftest import MockLLMProvider, MockCrawlAdapter` succeeds
    - `isinstance(MockLLMProvider([]), LLMProvider)` is `True` (runtime_checkable)
    - `isinstance(MockCrawlAdapter(), CrawlAdapter)` is `True` (runtime_checkable)
    - Each factory function returns a valid instance of its target type without arguments
    - `make_evidence()` produces an `Evidence` where `excerpt_hash == sha256(excerpt)`
    - `make_evidence(excerpt="custom")` recomputes hash for the custom excerpt
    - `uv run pytest --co` shows no import errors
