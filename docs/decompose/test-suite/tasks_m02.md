# Stage 4: Task Specifications — Milestone 02: Parsing & Models

> Foundation tests for the shared JSON extraction utility and Pydantic model constraints. Zero external dependencies.
>
> Fulfills: PDR-001 (pipeline logic first — parsing is the most critical shared utility)

---

- [ ] **T-02.01 — Write test_parsing.py (14 tests)**
  - **File:** `tests/test_parsing.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `extract_json` and `_repair_json` from `cce.parsing`
    - All tests are `@pytest.mark.unit`, synchronous (no async)
    - Tests for `extract_json()`:
      - `test_extract_json_direct_parse` — pass `'{"key": "value"}'`, expect `{"key": "value"}`
      - `test_extract_json_code_fence_json` — wrap valid JSON in `` ```json\n...\n``` ``, verify extraction
      - `test_extract_json_code_fence_plain` — wrap in `` ```\n...\n``` `` (no language tag)
      - `test_extract_json_preamble_and_postamble` — `"Here is the result:\n{...}\nLet me know"`, bracket-match fallback
      - `test_extract_json_nested_braces` — JSON with nested `{"inner": {"deep": 1}}`, find correct outer `{}`
      - `test_extract_json_returns_none_on_garbage` — `"not json at all"` → `None`
      - `test_extract_json_empty_string` — `""` → `None`
      - `test_extract_json_multiple_json_blocks` — text with two separate JSON objects, returns first
    - Tests for `_repair_json()`:
      - `test_repair_json_unescaped_quotes` — `'{"text": "the word "lost" here"}'` → repaired dict
      - `test_repair_json_multiple_unescaped` — multiple unescaped quotes in one value
      - `test_repair_json_returns_none_on_hopeless` — random bytes/text → `None`
      - `test_repair_json_max_repairs_limit` — input with >50 unescaped quotes → `None`, no hang
    - Integration between extract and repair:
      - `test_extract_json_triggers_repair` — code fence whose content has unescaped quotes, falls to repair path
      - `test_extract_json_crlf_normalization` — `"{\r\n\"key\": \"val\"\r\n}"` normalizes and parses
  - **Acceptance:**
    - `uv run pytest tests/test_parsing.py` passes all 14 tests
    - `uv run pytest tests/test_parsing.py -m unit` runs all 14 (none excluded)
    - No test takes >100ms

---

- [ ] **T-02.02 — Write test_models.py (5 tests)**
  - **File:** `tests/test_models/test_models.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import models from `cce.models.evidence`, `cce.models.content`, `cce.models.request`, `cce.models.job`
    - All tests are `@pytest.mark.unit`, synchronous
    - `test_evidence_frozen` — create `Evidence` via `make_evidence()`, attempt `ev.url = "new"`, assert raises `ValidationError` (Pydantic frozen model)
    - `test_source_quality_frozen` — create `SourceQuality(...)`, attempt mutation, assert raises
    - `test_content_scores_bounds` — `ContentScores(confidence=-0.1, coverage=0, source_diversity=0)` raises `ValidationError` (ge=0.0 constraint). Also test `confidence=1.1` (le=1.0 constraint)
    - `test_job_status_enum_values` — verify `JobStatus.QUEUED.value == "queued"`, `RUNNING == "running"`, `COMPLETED == "completed"`, `FAILED == "failed"`, `CANCELLED == "cancelled"`, `REVIEW_REQUIRED == "review_required"`
    - `test_curation_request_defaults` — `CurationRequest(topic="x", paths=["a"], policy_id="p")` has `audience == "general"`, `risk_profile == "medium"`, `subtopics == []`, `constraints is None`
  - **Acceptance:**
    - `uv run pytest tests/test_models/` passes all 5 tests
    - Frozen mutation tests raise `ValidationError`, not `AttributeError`
    - Bounds tests confirm Pydantic validates at construction time
