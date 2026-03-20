# Stage 4: Task Specifications — Milestone 05: Verifier & Writer

> Tests for the two LLM-calling agents. Uses `MockLLMProvider` to script responses and test JSON parsing, confidence scoring, and prompt construction.
>
> Fulfills: ADR-001 (protocol-level mocks), PDR-001 (pipeline logic first)

---

- [ ] **T-05.01 — Write test_verifier.py (13 tests)**
  - **File:** `tests/test_verification/test_verifier.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `Verifier`, `VerificationReport`, `ClaimVerification`, `Contradiction` from `cce.verification.verifier`
    - Import `MockLLMProvider`, `make_content_unit`, `make_evidence` from conftest
    - Import `LLMResponse` from `cce.llm.base`
    - **Unit tests** (`@pytest.mark.unit`, synchronous — test dataclasses and `_parse_response` directly):
      - `test_report_pass_rate_all_supported` — `VerificationReport(total_claims=5, supported=5)` → `pass_rate == 1.0`
      - `test_report_pass_rate_mixed` — `total=10, supported=6, gaps_acknowledged=2` → `pass_rate == 0.8`
      - `test_report_pass_rate_zero_claims` — `total=0` → `pass_rate == 0.0` (no division error, uses `max(1, total)`)
      - `test_parse_response_valid_json` — construct `Verifier(MockLLMProvider([]))`, call `verifier._parse_response(valid_json_string)` directly. Verify `report.claims` list length, `total_claims`, `supported`, `confidence_score`, `contradictions`, `overall_feedback`
      - `test_parse_response_non_json` — pass `"This is not JSON at all"` → report with `confidence_score == 0.0`
      - `test_confidence_no_penalties` — JSON with `supported=8, gaps=2, total=10, leakage=0, conflicts=0` → `confidence == 1.0`
      - `test_confidence_leakage_penalty` — `supported=7, gaps=1, total=10, leakage=2` → base `0.8`, multiplied by `max(0.0, 1.0 - (2/10)*1.5) = 0.7` → `confidence ≈ 0.56`
      - `test_confidence_conflict_penalty` — `supported=8, gaps=2, total=10, conflicts=1` → base `1.0 * 0.9 = 0.9`
      - `test_confidence_both_penalties` — both leakage and conflicts present → both penalties applied multiplicatively
      - `test_confidence_clamped` — extreme values don't produce confidence <0.0 or >1.0
      - `test_format_evidence` — `Verifier._format_evidence([ev1, ev2])` → string contains `[ev1.id] (URL: ev1.url)` and each excerpt
      - `test_parse_response_missing_summary` — JSON with `claims` array but no `summary` object → `total_claims == len(claims)`
    - **Integration test** (`@pytest.mark.integration`, async):
      - `test_verify_sends_correct_prompt` — create `Verifier(MockLLMProvider([valid_response]))`, call `await verifier.verify(unit, evidence)`. Inspect `llm.calls[0]`: system prompt is `VERIFIER_SYSTEM_PROMPT`, temperature is `0.1`, max_tokens is `16384`. User message contains the draft content and evidence block
  - **Acceptance:**
    - `uv run pytest tests/test_verification/test_verifier.py` passes all 13 tests
    - Unit tests call `_parse_response()` directly without async
    - Integration test uses `async def` and calls `verify()` through the mock
    - Confidence calculation tests use `pytest.approx()` for float comparison

---

- [ ] **T-05.02 — Write test_writer.py (13 tests)**
  - **File:** `tests/test_synthesis/test_writer.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `Writer`, `WriterOutput`, `_build_evidence_block` from `cce.synthesis.writer`
    - Import `MockLLMProvider`, `make_evidence`, `make_curation_request` from conftest
    - Import `LLMResponse` from `cce.llm.base`
    - **Unit tests** (`@pytest.mark.unit`, synchronous):
      - `test_build_evidence_block_formatting` — call `_build_evidence_block([ev1, ev2])`. Output contains `--- EVIDENCE [ev1.id] ---`, URL, title, author, published date, reputation tier, and excerpt text for each evidence
      - `test_build_evidence_block_optional_fields` — evidence with `title=None, author=None, published_at=None, source_quality=None` → no crash, missing fields omitted from output
      - `test_writer_no_evidence` — `await writer.write(request, evidence=[], path="blog")` → `WriterOutput` with `unit is None` and gaps containing a message about no evidence. Use `MockLLMProvider([])` (no LLM call should be made)
      - `test_parse_response_valid_json` — construct valid JSON with `content`, `citations_used`, `evidence_map`, `gaps`. Call `writer._parse_response(response, evidence, "blog", lineage)`. Verify `output.unit.content`, `output.unit.citations` (filtered to known IDs), `output.unit.evidence_map`, `output.unit.scores.source_diversity`
      - `test_parse_response_non_json_fallback` — `LLMResponse(content="Just plain markdown text")` → `WriterOutput` with `unit.content == "Just plain markdown text"`, `citations == []`, `evidence_map == []`, `scores.confidence == 0.0`
      - `test_parse_response_unknown_citation_ids_filtered` — `citations_used` contains `"ev_unknown"` not in evidence list → filtered out, only known IDs kept
      - `test_parse_response_empty_claims_filtered` — `evidence_map` contains entry with `claim: ""` → filtered out
      - `test_parse_response_diversity_calculation` — 3 evidence from 3 different URLs, LLM cites 2 → `source_diversity ≈ 0.67` (formula: `min(1.0, unique_cited / max(1, unique_available))`)
      - `test_parse_response_diversity_zero` — evidence provided but LLM cites nothing → `source_diversity == 0.0`
      - `test_writer_output_properties` — `WriterOutput(unit=unit, gaps=[], raw_response="")`: `has_content == True`, `has_gaps == False`. `WriterOutput(unit=None, gaps=["gap"], raw_response="")`: `has_content == False`, `has_gaps == True`
    - **Integration tests** (`@pytest.mark.integration`, async):
      - `test_write_sends_correct_prompt` — `await writer.write(request, evidence, "blog")`. Inspect `llm.calls[0]`: system prompt is `WRITER_SYSTEM_PROMPT`, user message contains topic, subtopics, audience, path, evidence block, evidence count
      - `test_write_with_feedback` — call with `feedback="Fix claim X"`. User prompt contains `"VERIFIER FEEDBACK"` marker and the feedback text
      - `test_write_temperature` — verify `llm.calls[0]["temperature"] == 0.2`
  - **Acceptance:**
    - `uv run pytest tests/test_synthesis/test_writer.py` passes all 13 tests
    - `test_writer_no_evidence` confirms no LLM call is made (`len(llm.calls) == 0`)
    - Diversity tests use `pytest.approx()` for float comparison
    - `test_parse_response_valid_json` verifies citations are `Citation` objects with correct `evidence_id` and `url`
