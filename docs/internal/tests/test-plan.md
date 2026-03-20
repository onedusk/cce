# CCE Test Plan

## Current State

- **Coverage:** 0% — only an empty `tests/__init__.py` exists
- **Infrastructure ready:** pytest + pytest-asyncio configured in `pyproject.toml` with `asyncio_mode = "auto"`
- **Source modules:** ~33 files across 8 packages implementing the full Phase 1 pipeline

---

## Test Directory Structure

Mirrors `src/cce/` layout:

```
tests/
    __init__.py                        (exists)
    conftest.py                        (shared fixtures)
    test_parsing.py
    test_output.py
    test_models/
        __init__.py
        test_models.py
    test_config/
        __init__.py
        test_loader.py
    test_policy/
        __init__.py
        test_loader.py
    test_discovery/
        __init__.py
        test_discoverer.py
    test_evidence/
        __init__.py
        test_sqlite.py
    test_synthesis/
        __init__.py
        test_writer.py
    test_verification/
        __init__.py
        test_verifier.py
        test_gate.py
    test_orchestrator/
        __init__.py
        test_pipeline.py
```

---

## Pytest Markers

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "unit: Fast tests with no I/O or external dependencies",
    "integration: Tests using real I/O (SQLite, filesystem) with mocked external services",
    "slow: Tests that take >1s (multi-iteration pipeline loops)",
    "e2e: End-to-end tests requiring real ANTHROPIC_API_KEY and FIRECRAWL_API_KEY",
]
```

- Default CI: `uv run pytest -m "not e2e"`
- Fast feedback: `uv run pytest -m unit` (~100 tests, <5s)
- `e2e` marker defined now but tests deferred to later

---

## Shared Fixtures (`tests/conftest.py`)

### Model Factories

| Factory | Purpose |
|---------|---------|
| `make_evidence(**overrides)` | Build `Evidence` with sensible defaults; auto-computes `excerpt_hash` from `excerpt` if not given |
| `make_curation_request(**overrides)` | Build `CurationRequest` with `topic="test topic"`, `paths=["blog"]`, `risk_profile="medium"` |
| `make_source_policy(**overrides)` | Build `SourcePolicy` with empty allow/deny lists, reasonable reputation/recency rules |
| `make_content_unit(**overrides)` | Build `ContentUnit` with zero scores and default lineage |
| `make_verification_report(**overrides)` | Build `VerificationReport` with configurable claim counts |

### MockLLMProvider

Satisfies the `LLMProvider` protocol. Holds a list of `LLMResponse` objects, popping one per `.complete()` call. Records all calls for assertion (messages, system prompt, temperature, max_tokens). Supports multi-turn sequences for writer-verifier loop testing.

### MockCrawlAdapter

Satisfies the `CrawlAdapter` protocol. Maps URLs to `CrawlResult` objects for `crawl()`/`crawl_many()`, and query strings to URL lists for `search()`. Makes test data self-documenting.

### Temp SQLite Store

Fixture `sqlite_store` creates a `SQLiteEvidenceStore` with `tmp_path / "test.db"`, calls `await store.connect()`, yields, then `await store.close()`. Each test gets an isolated database.

### Config Fixtures

- `default_engine_config()` — `EngineConfig` with dummy API keys and in-memory-friendly paths
- `default_gate_config()` — `QualityGateConfig` with explicit thresholds for deterministic testing

---

## Per-Module Test Breakdown

### 1. `test_parsing.py` — `src/cce/parsing.py`

Shared utility used by both Writer and Verifier. All unit tests, no I/O.

| # | Test | Type |
|---|------|------|
| 1 | `test_extract_json_direct_parse` — valid JSON string returns dict | unit |
| 2 | `test_extract_json_code_fence_json` — `` ```json {...} ``` `` extracts correctly | unit |
| 3 | `test_extract_json_code_fence_plain` — `` ``` {...} ``` `` (no language tag) | unit |
| 4 | `test_extract_json_preamble_and_postamble` — text before/after JSON, bracket-match fallback | unit |
| 5 | `test_extract_json_nested_braces` — correct outer `{}` found with nesting | unit |
| 6 | `test_extract_json_returns_none_on_garbage` — non-JSON text returns `None` | unit |
| 7 | `test_extract_json_empty_string` — empty string returns `None` | unit |
| 8 | `test_extract_json_multiple_json_blocks` — returns first block | unit |
| 9 | `test_repair_json_unescaped_quotes` — fixes inner unescaped quotes | unit |
| 10 | `test_repair_json_multiple_unescaped` — multiple unescaped quotes in one value | unit |
| 11 | `test_repair_json_returns_none_on_hopeless` — completely malformed → `None` | unit |
| 12 | `test_repair_json_max_repairs_limit` — >50 issues returns `None`, no infinite loop | unit |
| 13 | `test_extract_json_triggers_repair` — code fence with bad quotes falls through to repair | unit |
| 14 | `test_extract_json_crlf_normalization` — `\r\n` line endings handled | unit |

### 2. `test_discovery/test_discoverer.py` — `src/cce/discovery/discoverer.py`

Largest test file. Static methods are pure unit tests; `discover()` is integration.

**Unit tests (static methods, no I/O):**

| # | Test | Type |
|---|------|------|
| 1 | `test_build_queries_topic_only` — no subtopics → `[topic]` | unit |
| 2 | `test_build_queries_with_subtopics` — produces `[topic, "topic sub1", ...]` | unit |
| 3 | `test_passes_policy_allow_all` — empty allow + empty deny → any URL passes | unit |
| 4 | `test_passes_policy_deny_blocks` — URL matching deny list is rejected | unit |
| 5 | `test_passes_policy_deny_takes_priority` — matches both → rejected (deny wins) | unit |
| 6 | `test_passes_policy_allow_list_gates` — non-matching URL rejected when allow list non-empty | unit |
| 7 | `test_passes_policy_allow_list_passes` — matching URL accepted | unit |
| 8 | `test_passes_policy_no_domain` — malformed URL → `False` | unit |
| 9 | `test_passes_policy_case_insensitive` — domain matching ignores case | unit |
| 10 | `test_resolve_overrides_no_match` — topic doesn't match → original policy returned | unit |
| 11 | `test_resolve_overrides_match_merges` — matching override merges allow/deny, overrides reputation/recency | unit |
| 12 | `test_resolve_overrides_no_recursion` — returned policy has `topic_overrides=[]` | unit |
| 13 | `test_chunk_content_empty` — empty markdown → `[]` | unit |
| 14 | `test_chunk_content_single_paragraph` — short text → one chunk | unit |
| 15 | `test_chunk_content_paragraph_split` — `\n\n` separated → two chunks | unit |
| 16 | `test_chunk_content_long_paragraph` — oversized paragraph splits on `\n` | unit |
| 17 | `test_looks_peer_reviewed_doi` — `doi.org` URL → `True` | unit |
| 18 | `test_looks_peer_reviewed_pubmed` — `pubmed` URL → `True` | unit |
| 19 | `test_looks_peer_reviewed_normal` — normal URL → `False` | unit |
| 20 | `test_looks_primary_gov_edu_org` — `.gov`/`.edu`/`.org` → `True` | unit |
| 21 | `test_looks_primary_com` — `.com` → `False` | unit |
| 22 | `test_assess_reputation_trusted` — trusted institution URL → `"trusted"` | unit |
| 23 | `test_assess_reputation_institutional` — `.gov`/`.edu` (not trusted) → `"institutional"` | unit |
| 24 | `test_assess_reputation_unknown` — `.com` → `"unknown"` | unit |
| 25 | `test_looks_marketing_positive` — "buy now" text → `True` | unit |
| 26 | `test_looks_marketing_negative` — academic text → `False` | unit |
| 27 | `test_extract_evidence_creates_objects` — correct fields: url, title, excerpt_hash, locator, source_quality | unit |
| 28 | `test_extract_evidence_published_at_parsing` — ISO 8601 dates parsed, invalid → `None` | unit |

**Integration tests (mocked adapter):**

| # | Test | Type |
|---|------|------|
| 29 | `test_discover_full_flow` — search → filter → crawl → extract; assert evidence count and fields | integration |
| 30 | `test_discover_no_urls_after_filter` — all URLs blocked → empty list | integration |
| 31 | `test_discover_empty_crawl_skipped` — `status_code=0` or empty markdown skipped | integration |
| 32 | `test_discover_max_sources_cap` — `max_sources_per_run=2` limits crawled URLs | integration |
| 33 | `test_discover_dedup_by_hash` — duplicate excerpts across sources produce single Evidence | integration |

### 3. `test_synthesis/test_writer.py` — `src/cce/synthesis/writer.py`

| # | Test | Type |
|---|------|------|
| 1 | `test_build_evidence_block_formatting` — output contains `--- EVIDENCE [id] ---`, URL, title, author, excerpt | unit |
| 2 | `test_build_evidence_block_optional_fields` — `None` title/author/date → no crash, fields omitted | unit |
| 3 | `test_writer_no_evidence` — empty evidence list → `WriterOutput(unit=None)` with gap message | unit |
| 4 | `test_parse_response_valid_json` — well-formed JSON → correct citations, claim mappings, diversity | unit |
| 5 | `test_parse_response_non_json_fallback` — plain markdown → fallback with `confidence=0.0` | unit |
| 6 | `test_parse_response_unknown_citation_ids_filtered` — LLM cites non-existent evidence IDs → filtered out | unit |
| 7 | `test_parse_response_empty_claims_filtered` — empty claim strings in evidence_map → filtered out | unit |
| 8 | `test_parse_response_diversity_calculation` — 2 of 3 URLs cited → diversity = 0.67 | unit |
| 9 | `test_parse_response_diversity_zero` — nothing cited → diversity = 0.0 | unit |
| 10 | `test_writer_output_properties` — `has_content` / `has_gaps` return correct booleans | unit |
| 11 | `test_write_sends_correct_prompt` — system prompt, user prompt structure, evidence block inclusion | integration |
| 12 | `test_write_with_feedback` — feedback string appended with "VERIFIER FEEDBACK" markers | integration |
| 13 | `test_write_temperature` — LLM called with `temperature=0.2` | integration |

### 4. `test_verification/test_verifier.py` — `src/cce/verification/verifier.py`

| # | Test | Type |
|---|------|------|
| 1 | `test_report_pass_rate_all_supported` — 5/5 supported → 1.0 | unit |
| 2 | `test_report_pass_rate_mixed` — 6 supported + 2 gaps of 10 → 0.8 | unit |
| 3 | `test_report_pass_rate_zero_claims` — 0 total → 0.0, no division error | unit |
| 4 | `test_parse_response_valid_json` — all fields populated correctly | unit |
| 5 | `test_parse_response_non_json` — returns `confidence_score=0.0` | unit |
| 6 | `test_confidence_no_penalties` — 8 supported + 2 gaps of 10 → 1.0 | unit |
| 7 | `test_confidence_leakage_penalty` — leakage multiplier applied correctly | unit |
| 8 | `test_confidence_conflict_penalty` — conflict multiplier applied correctly | unit |
| 9 | `test_confidence_both_penalties` — both penalties stack multiplicatively | unit |
| 10 | `test_confidence_clamped` — never exceeds 1.0 or goes below 0.0 | unit |
| 11 | `test_format_evidence` — `[ev_id] (URL: ...)` format with excerpt | unit |
| 12 | `test_parse_response_missing_summary` — claims list present but no summary → `total_claims = len(claims)` | unit |
| 13 | `test_verify_sends_correct_prompt` — system prompt, temperature=0.1, max_tokens=16384 | integration |

### 5. `test_verification/test_gate.py` — `src/cce/verification/gate.py`

All unit tests — pure decision logic, no I/O.

| # | Test | Type |
|---|------|------|
| 1 | `test_gate_pass_high_confidence` — confidence >= threshold, no leakage → PASS | unit |
| 2 | `test_gate_fail_low_confidence_fixable` — low confidence, fixable, under max iter → FAIL | unit |
| 3 | `test_gate_review_max_iterations` — at max iterations → REVIEW | unit |
| 4 | `test_gate_review_no_fixable_issues` — low confidence, nothing fixable → REVIEW | unit |
| 5 | `test_gate_fail_leakage_blocks_pass` — confidence OK but leakage > 0 → not PASS | unit |
| 6 | `test_gate_feedback_unsupported` — feedback string mentions unsupported count | unit |
| 7 | `test_gate_feedback_uncited` — feedback string mentions uncited count | unit |
| 8 | `test_gate_feedback_leakage` — feedback string mentions leakage count | unit |
| 9 | `test_gate_feedback_conflicts` — feedback string mentions contradiction count | unit |
| 10 | `test_gate_feedback_citation_density` — low density ratio mentioned in feedback | unit |
| 11 | `test_gate_feedback_no_issues` — "No issues found." | unit |
| 12 | `test_check_citation_density_empty` — empty content → `(False, 0.0)` | unit |
| 13 | `test_check_citation_density_short_paragraphs_skipped` — <=15 words not counted as substantive | unit |
| 14 | `test_check_citation_density_headings_skipped` — `#` lines excluded | unit |
| 15 | `test_check_citation_density_ev_colon_format` — `[ev:abc123]` matched | unit |
| 16 | `test_check_citation_density_ev_underscore_format` — `[ev_abc123]` matched | unit |
| 17 | `test_check_citation_density_multiple_required` — config requires 2 per paragraph | unit |
| 18 | `test_has_fixable_issues_true` — any of unsupported/uncited/leakage/conflicts > 0 | unit |
| 19 | `test_has_fixable_issues_false` — all zero → `False` | unit |
| 20 | `test_gate_result_properties` — `should_rewrite`/`should_publish`/`needs_human` correct per decision | unit |

### 6. `test_evidence/test_sqlite.py` — `src/cce/evidence/sqlite.py`

All async integration tests using real SQLite via `tmp_path`.

| # | Test | Type |
|---|------|------|
| 1 | `test_connect_creates_schema` — `evidence` + `_meta` tables exist, version = 1 | integration |
| 2 | `test_put_and_get` — store one, retrieve by ID, fields match | integration |
| 3 | `test_put_returns_true` — new evidence → `True` | integration |
| 4 | `test_put_duplicate_hash_returns_false` — same excerpt_hash → `False` | integration |
| 5 | `test_put_many` — 5 objects → returns 5 | integration |
| 6 | `test_put_many_dedup` — 3 unique + 2 duplicate hashes → returns 3 | integration |
| 7 | `test_get_nonexistent` — returns `None` | integration |
| 8 | `test_get_many` — store 3, retrieve 2 → returns 2 | integration |
| 9 | `test_get_many_empty_list` — `[]` → `[]` | integration |
| 10 | `test_get_many_partial_miss` — 3 requested, 2 exist → returns 2 | integration |
| 11 | `test_search_by_url` — prefix match on URL | integration |
| 12 | `test_search_by_topic` — title/excerpt keyword match | integration |
| 13 | `test_search_limit` — 10 stored, `limit=3` → 3 returned | integration |
| 14 | `test_search_no_filters` — returns all up to default limit | integration |
| 15 | `test_exists_by_hash_true` — stored hash → `True` | integration |
| 16 | `test_exists_by_hash_false` — absent hash → `False` | integration |
| 17 | `test_count` — 7 stored → returns 7 | integration |
| 18 | `test_serialization_roundtrip` — all fields including `SourceQuality` survive roundtrip | integration |
| 19 | `test_serialization_nullable_fields` — `None` title/author/date/quality preserved | integration |

### 7. `test_config/test_loader.py` — `src/cce/config/loader.py`

| # | Test | Type |
|---|------|------|
| 1 | `test_load_config_defaults` — no args → expected defaults | unit |
| 2 | `test_load_config_from_yaml` — YAML values override defaults | unit |
| 3 | `test_load_config_env_overrides_yaml` — env var wins over YAML | unit |
| 4 | `test_load_config_env_var_fallback_chain` — `ANTHROPIC_API_KEY` fallback | unit |
| 5 | `test_load_config_missing_yaml` — nonexistent path → uses defaults | unit |
| 6 | `test_load_gate_config_defaults` — no gate section → low/medium/high defaults | unit |
| 7 | `test_load_gate_config_custom_profile` — custom profile appears alongside defaults | unit |
| 8 | `test_load_config_type_coercion` — string env vars → correct numeric types | unit |

### 8. `test_policy/test_loader.py` — `src/cce/policy/loader.py`

| # | Test | Type |
|---|------|------|
| 1 | `test_load_policy_minimal` — just id + name → defaults for everything else | unit |
| 2 | `test_load_policy_full` — all fields parsed correctly | unit |
| 3 | `test_load_policies_directory` — 2 YAML files → dict with 2 entries | unit |
| 4 | `test_load_policies_skips_invalid` — malformed YAML logged and skipped | unit |
| 5 | `test_parse_policy_topic_overrides` — `TopicOverride` constructed correctly | unit |
| 6 | `test_load_real_peer_reviewed_policy` — actual `policies/peer-reviewed.yaml` loads correctly | integration |

### 9. `test_orchestrator/test_pipeline.py` — `src/cce/orchestrator/pipeline.py`

All integration tests with mocked LLM + mocked adapter + real SQLite.

| # | Test | Type |
|---|------|------|
| 1 | `test_pipeline_happy_path` — 3 sources, good writer/verifier → COMPLETED, package has 1 unit | integration |
| 2 | `test_pipeline_no_evidence` — empty search → FAILED with error message | integration |
| 3 | `test_pipeline_single_pass` — gate PASS on iteration 1, no loop | integration |
| 4 | `test_pipeline_rewrite_loop` — first attempt FAIL, second PASS, feedback forwarded | integration |
| 5 | `test_pipeline_review_max_iterations` — never passes → REVIEW_REQUIRED | integration, slow |
| 6 | `test_pipeline_multiple_paths` — 2 output paths → package has 2 units | integration |
| 7 | `test_pipeline_exception_handling` — adapter throws → FAILED with error | integration |
| 8 | `test_update_job_status_transitions` — status, updated_at, completed_at, error set correctly | unit |

### 10. `test_output.py` — `src/cce/output.py`

| # | Test | Type |
|---|------|------|
| 1 | `test_serialize_result_completed` — dict has status/job/package/gate_results keys | unit |
| 2 | `test_serialize_result_failed` — `package` is `None` | unit |
| 3 | `test_serialize_handles_datetime` — ISO 8601 strings | unit |
| 4 | `test_serialize_handles_enums` — `GateDecision.PASS` → `"pass"` | unit |
| 5 | `test_serialize_handles_paths` — `Path` → string | unit |
| 6 | `test_write_output_creates_files` — result.json, content.md, evidence.json, verification.json | integration |
| 7 | `test_write_output_result_json_valid` — parseable JSON matching serialize output | integration |
| 8 | `test_write_output_no_package` — failed result → files still created | integration |

### 11. `test_models/test_models.py` — `src/cce/models/`

Selective validation only — not testing basic Pydantic field presence.

| # | Test | Type |
|---|------|------|
| 1 | `test_evidence_frozen` — mutation raises error | unit |
| 2 | `test_source_quality_frozen` — mutation raises error | unit |
| 3 | `test_content_scores_bounds` — out-of-range values rejected | unit |
| 4 | `test_job_status_enum_values` — correct string values | unit |
| 5 | `test_curation_request_defaults` — audience, risk_profile, subtopics defaults | unit |

---

## Implementation Order

Build from leaves (no dependencies) toward the root (orchestrator):

### Phase A — Foundations

| Order | File | Tests | Rationale |
|-------|------|-------|-----------|
| 1 | `conftest.py` | — | All other files depend on shared fixtures |
| 2 | `test_parsing.py` | ~14 | Zero deps, pure functions, used by Writer + Verifier |
| 3 | `test_models/test_models.py` | ~5 | Quick wins, validates frozen constraints |

### Phase B — Core Static Logic

| Order | File | Tests | Rationale |
|-------|------|-------|-----------|
| 4 | `test_discovery/test_discoverer.py` (unit only) | ~28 | Highest-value: protects domain logic on every pipeline run |
| 5 | `test_verification/test_gate.py` | ~20 | Pure decision logic, determines ship/no-ship |

### Phase C — Mocked-LLM Components

| Order | File | Tests | Rationale |
|-------|------|-------|-----------|
| 6 | `test_verification/test_verifier.py` | ~13 | Parse + confidence scoring with mock LLM |
| 7 | `test_synthesis/test_writer.py` | ~13 | Parse + evidence formatting with mock LLM |

### Phase D — Storage & Config

| Order | File | Tests | Rationale |
|-------|------|-------|-----------|
| 8 | `test_evidence/test_sqlite.py` | ~19 | Real SQLite via tmp_path, validates data layer |
| 9 | `test_config/test_loader.py` | ~8 | Env var precedence, YAML loading |
| 10 | `test_policy/test_loader.py` | ~6 | YAML parsing |

### Phase E — Integration

| Order | File | Tests | Rationale |
|-------|------|-------|-----------|
| 11 | `test_discovery/test_discoverer.py` (add integration) | ~5 | Full discover() flow with MockCrawlAdapter |
| 12 | `test_orchestrator/test_pipeline.py` | ~8 | Full pipeline with all mocks wired together |
| 13 | `test_output.py` | ~8 | Serialization + filesystem writes |

**Total: ~147 tests across 11 test files + 1 conftest**

---

## Mocking Strategy

### LLM Responses

Mock at the **protocol level** via `MockLLMProvider`, not at the `anthropic` SDK level. For each test, construct the exact JSON string the LLM would return, wrapped in `LLMResponse(content=json_string)`. This:
- Tests actual parsing logic in Writer/Verifier
- Is provider-agnostic
- Naturally supports edge cases (malformed JSON, empty responses)

For multi-turn tests (write-verify loop), stack multiple responses in order.

### Crawl Adapter

`MockCrawlAdapter` maps URLs → `CrawlResult` and queries → URL lists. More explicit than patching, makes test data self-documenting.

### SQLite

Use **real SQLite** via `tmp_path` — in-process `aiosqlite` is fast enough. Each test gets an isolated DB file. Mocking SQL would give false confidence.

### Environment Variables

Use `monkeypatch.setenv()` / `monkeypatch.delenv()` for config tests. Never set env vars globally.

### Filesystem

Use `tmp_path` (built-in pytest fixture) for all file I/O tests.

### What NOT to Mock

- Pydantic model construction
- `hashlib`, `json.loads`, `re.search` — deterministic stdlib functions
- SQLite (use real in-memory/tmp instances instead)
