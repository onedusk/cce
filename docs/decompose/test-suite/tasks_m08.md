# Stage 4: Task Specifications — Milestone 08: Pipeline & Output Integration

> Integration tests that wire all components together. Discovery integration with `MockCrawlAdapter`, full pipeline orchestration, and output file generation.
>
> Fulfills: PDR-001 (integration tests last, after all unit tests are green)

---

- [ ] **T-08.01 — Add discovery integration tests (5 tests)**
  - **File:** `tests/test_discovery/test_discoverer.py` (MODIFY)
  - **Depends on:** T-03.01
  - **Outline:**
    - Add a new section at the bottom of the existing file: `# --- Integration tests ---`
    - Import `MockCrawlAdapter`, `make_curation_request`, `make_source_policy`, `make_crawl_result` from conftest
    - Import `CrawlConfig` from `cce.config.types`
    - All tests are `@pytest.mark.integration`, `async def`
    - `test_discover_full_flow` — configure `MockCrawlAdapter` with `search_map` returning 2 URLs and `url_map` with valid `CrawlResult` for each. Create `Discoverer(adapter, CrawlConfig(...))`. Call `await discoverer.discover(request, policy)`. Assert: evidence list is non-empty, each evidence has correct `url`, `excerpt_hash` is SHA-256, `source_quality` is populated
    - `test_discover_no_urls_after_filter` — `search_map` returns URLs, but all are in `policy.domains_deny`. Result: empty list
    - `test_discover_empty_crawl_skipped` — `url_map` returns `CrawlResult(status_code=0, markdown="")` for a URL. That URL produces no evidence (skipped)
    - `test_discover_max_sources_cap` — `search_map` returns 10 URLs, `policy.max_sources_per_run=2`. Only 2 URLs are crawled (check `len(adapter)` calls or evidence URL uniqueness)
    - `test_discover_dedup_by_hash` — two different URLs return identical `markdown` content. Evidence list contains each excerpt only once (dedup by `excerpt_hash`)
  - **Acceptance:**
    - `uv run pytest tests/test_discovery/test_discoverer.py -m integration` passes all 5 new tests
    - `uv run pytest tests/test_discovery/test_discoverer.py` passes all 33 tests (28 unit + 5 integration)
    - Existing unit tests are unaffected by the additions
    - `test_discover_full_flow` asserts on evidence field values, not just list length

---

- [ ] **T-08.02 — Write test_pipeline.py (8 tests)**
  - **File:** `tests/test_orchestrator/test_pipeline.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `Pipeline`, `PipelineResult` from `cce.orchestrator.pipeline`
    - Import `MockLLMProvider`, `MockCrawlAdapter`, `make_engine_config`, `make_curation_request`, `make_source_policy`, `make_crawl_result`, `make_evidence` from conftest
    - Use `sqlite_store` fixture for real SQLite
    - Import `LLMResponse` from `cce.llm.base`
    - Import `JobStatus` from `cce.models.job`
    - All tests are `@pytest.mark.integration`, `async def`
    - For each test, construct the full `Pipeline(config, adapter, store, llm)` and call `await pipeline.run(request, policy)`
    - Prepare JSON response strings for writer and verifier (valid JSON matching their expected formats)
    - Note: pipeline iterations are **1-indexed** (`range(1, max_iters + 1)`), so iteration 1 is first, iteration 3 == max for medium profile
    - `test_pipeline_happy_path` — adapter returns 3 sources, LLM returns valid writer JSON (high-quality content) then valid verifier JSON (high confidence, all supported). Assert: `result.succeeded == True`, `result.package is not None`, `result.package.units` has 1 unit, `result.job.status == JobStatus.COMPLETED`
    - `test_pipeline_no_evidence` — adapter `search_map` returns empty list. Assert: `result.failed == True`, `result.job.status == JobStatus.FAILED`
    - `test_pipeline_single_pass` — writer + verifier produce PASS on first iteration. Assert: 1 iteration (check `len(result.gate_results) == 1`), decision is PASS
    - `test_pipeline_rewrite_loop` — first writer/verifier round → FAIL (low confidence, fixable). Second round → PASS. Stack 4 LLM responses (writer1, verifier1, writer2, verifier2). Assert: `len(result.gate_results) >= 2`, final decision is PASS, second writer call includes feedback
    - `test_pipeline_review_max_iterations` — every verifier response returns low confidence. After `max_writer_iterations` loops, gate returns REVIEW. Assert: `result.needs_review == True`, `result.job.status == JobStatus.REVIEW_REQUIRED`
    - `test_pipeline_multiple_paths` — `request.paths = ["blog", "newsletter"]`. Stack LLM responses for both paths (writer+verifier per path). Assert: `len(result.package.units) == 2`, each unit has correct `path`
    - `test_pipeline_exception_handling` — adapter raises `RuntimeError("Network error")` during crawl. Assert: `result.failed == True`, `result.job.error` is not None
    - `test_update_job_status_transitions` (`@pytest.mark.unit`, synchronous) — call `Pipeline._update_job(job, COMPLETED)`. Assert: `job.status == COMPLETED`, `job.completed_at is not None`. Call with `FAILED` + `error_msg="oops"`. Assert: `job.error.message == "oops"`
  - **Acceptance:**
    - `uv run pytest tests/test_orchestrator/test_pipeline.py` passes all 8 tests
    - `test_pipeline_rewrite_loop` verifies feedback was passed to the writer on the second call by inspecting `llm.calls[2]` (third call = second writer call)
    - `test_pipeline_review_max_iterations` is marked `@pytest.mark.slow` in addition to `integration`
    - `test_update_job_status_transitions` is the only synchronous test in this file

---

- [ ] **T-08.03 — Write test_output.py (8 tests)**
  - **File:** `tests/test_output.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `serialize_result`, `write_output` from `cce.output`
    - Import `PipelineResult` from `cce.orchestrator.pipeline`
    - Import `make_content_unit`, `make_evidence`, `make_verification_report`, `make_gate_config` from conftest
    - Import `GateResult`, `GateDecision` from `cce.verification.gate`
    - Import `Job`, `JobStatus` from `cce.models.job`
    - Import `PublishPackage` from `cce.models.package`
    - Use `tmp_path` for file output tests
    - **Unit tests** (`@pytest.mark.unit`, synchronous):
      - `test_serialize_result_completed` — build `PipelineResult` with a package. `serialize_result()` returns dict with keys `status`, `job`, `package`, `gate_results`. `status` is string `"completed"`, not enum
      - `test_serialize_result_failed` — failed result (`package=None`). `result["package"] is None`
      - `test_serialize_handles_datetime` — result dict contains datetime fields as ISO 8601 strings (not `datetime` objects)
      - `test_serialize_handles_enums` — `GateDecision.PASS` serialized as `"pass"`, `JobStatus.COMPLETED` as `"completed"`
      - `test_serialize_handles_paths` — `Path("evidence.db")` serialized as `"evidence.db"` (string)
    - **Integration tests** (`@pytest.mark.integration`, synchronous):
      - `test_write_output_creates_files` — call `write_output(result, tmp_path)`. Assert: returned path is a directory containing `result.json`, `content.md`, `evidence.json`, `verification.json`
      - `test_write_output_result_json_valid` — read `result.json`, parse as JSON. Verify it matches `serialize_result()` output
      - `test_write_output_no_package` — failed result with `package=None`. Files still created, `content.md` contains `"(no content)"` or similar indicator
  - **Acceptance:**
    - `uv run pytest tests/test_output.py` passes all 8 tests
    - Serialization tests verify type conversions, not just key presence
    - `test_write_output_creates_files` uses `tmp_path` and checks actual file existence with `Path.exists()`
    - `test_write_output_result_json_valid` round-trips through `json.loads()` and compares structure
