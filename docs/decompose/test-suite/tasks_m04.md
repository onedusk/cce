# Stage 4: Task Specifications — Milestone 04: Quality Gate

> Unit tests for the quality gate decision logic — the binary ship/no-ship decision point. Pure logic, no mocks needed beyond constructing `VerificationReport` instances.

---

- [ ] **T-04.01 — Write test_gate.py (20 tests)**
  - **File:** `tests/test_verification/test_gate.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `QualityGate`, `GateDecision`, `GateResult` from `cce.verification.gate`
    - Import `make_gate_config`, `make_content_unit`, `make_verification_report` from conftest
    - All tests are `@pytest.mark.unit`, synchronous
    - **Decision routing** (`QualityGate.evaluate`):
      - `test_gate_pass_high_confidence` — `confidence_score=0.9` (≥0.85 threshold), `leakage=0`, citation OK → `GateDecision.PASS`
      - `test_gate_fail_low_confidence_fixable` — `confidence_score=0.5`, `unsupported=3`, `iteration=1` (< max 3) → `GateDecision.FAIL`
      - `test_gate_review_max_iterations` — `iteration=3` (== max), any confidence → `GateDecision.REVIEW`. Feedback contains "Max iterations" or similar
      - `test_gate_review_no_fixable_issues` — `confidence_score=0.5`, all counts zero (unsupported=0, uncited=0, leakage=0, conflicts=0), `iteration=1` → `GateDecision.REVIEW` (nothing to fix)
      - `test_gate_fail_leakage_blocks_pass` — `confidence_score=0.95`, `leakage=1`, citation OK → NOT `GateDecision.PASS` (leakage blocks autopublish)
    - **Feedback generation** (feedback string in `GateResult`):
      - `test_gate_feedback_unsupported` — `unsupported=3` → feedback contains `"3"` and `"don't match"`
      - `test_gate_feedback_uncited` — `uncited=2` → feedback contains `"2"` and `"no citations"`
      - `test_gate_feedback_leakage` — `leakage=1` → feedback contains `"1"` and `"training data"`
      - `test_gate_feedback_conflicts` — `conflicts=2` → feedback contains `"2"` and `"contradiction"`
      - `test_gate_feedback_citation_density` — construct `ContentUnit` with a paragraph that has zero `[ev:...]` citations. Gate config requires `min_citations_per_paragraph=1`. Feedback mentions density ratio
      - `test_gate_feedback_no_issues` — all counts zero, high confidence, good citations → feedback is `"No issues found."` or empty
    - **Citation density** (`QualityGate._check_citation_density`):
      - `test_check_citation_density_empty` — `content=""` → returns `(True, 1.0)` (no substantive paragraphs = vacuously true)
      - `test_check_citation_density_short_paragraphs_skipped` — paragraph with ≤15 words not counted as substantive
      - `test_check_citation_density_headings_skipped` — lines starting with `#` are not paragraphs
      - `test_check_citation_density_ev_colon_format` — paragraph containing `[ev:abc123]` is counted as cited
      - `test_check_citation_density_ev_underscore_format` — paragraph containing `[ev_abc123]` is also counted (regex `\[ev[_:][^\]]+\]`)
      - `test_check_citation_density_multiple_required` — config `min_citations_per_paragraph=2`, paragraph with 1 citation → that paragraph fails, paragraph with 2 → passes
    - **Utility methods**:
      - `test_has_fixable_issues_true` — any of unsupported/uncited/leakage/conflicts > 0 → `True`
      - `test_has_fixable_issues_false` — all zero → `False`
    - **GateResult properties**:
      - `test_gate_result_properties` — `GateResult(decision=PASS, ...)`: `should_publish=True`, `should_rewrite=False`, `needs_human=False`. Also verify FAIL → `should_rewrite=True` and REVIEW → `needs_human=True`
  - **Acceptance:**
    - `uv run pytest tests/test_verification/test_gate.py` passes all 20 tests
    - All tests are synchronous (no async)
    - `uv run pytest tests/test_verification/test_gate.py -m unit` runs all 20
    - Each decision routing test asserts on `result.decision` enum value, not string
