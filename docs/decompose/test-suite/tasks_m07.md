# Stage 4: Task Specifications — Milestone 07: Config & Policy

> Tests for YAML loading, environment variable precedence, and policy parsing.

---

- [ ] **T-07.01 — Write test_config/test_loader.py (8 tests)**
  - **File:** `tests/test_config/test_loader.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `load_config` from `cce.config.loader`
    - Import config types from `cce.config.types`
    - Use `tmp_path` for YAML files, `monkeypatch` for env vars
    - All tests are `@pytest.mark.unit`, synchronous
    - `test_load_config_defaults` — call `load_config()` with no args, no env vars set (use `monkeypatch.delenv` for `ANTHROPIC_API_KEY` etc.). Verify defaults: `provider == "anthropic"`, `model == "claude-sonnet-4-6"`, `backend == "sqlite"`, `adapter == "firecrawl"`. Gate configs exist for `"low"`, `"medium"`, `"high"`
    - `test_load_config_from_yaml` — write minimal YAML to `tmp_path / "config.yaml"` with `llm: {model: "claude-opus-4-6"}`. Call `load_config(path)`. Verify model is overridden
    - `test_load_config_env_overrides_yaml` — YAML sets `model: "claude-haiku-4-5-20251001"`, env var `CCE_LLM_MODEL=claude-opus-4-6`. Verify env var wins
    - `test_load_config_env_var_fallback_chain` — `CCE_LLM_API_KEY` not set, `ANTHROPIC_API_KEY=test-key`. Verify `config.llm.api_key == "test-key"`
    - `test_load_config_missing_yaml` — call with path to nonexistent file → does not crash, uses defaults
    - `test_load_gate_config_defaults` — no gate section in YAML. Returns dict with `"low"`, `"medium"`, `"high"` profiles with expected thresholds (low: 0.7/citations=1/iters=2, medium: 0.85/citations=1/iters=3, high: 0.95/citations=2/iters=4). Verify `min_citations_per_paragraph` differs between profiles
    - `test_load_gate_config_custom_profile` — YAML with `quality_gate: {ultra: {autopublish_threshold: 0.99}}`. Verify `"ultra"` profile exists alongside defaults
    - `test_load_config_type_coercion` — env vars are strings. Set `CCE_LLM_TEMPERATURE=0.5`. Verify `config.llm.temperature` is a `float`, not a `str`
  - **Acceptance:**
    - `uv run pytest tests/test_config/test_loader.py` passes all 8 tests
    - Tests that set env vars use `monkeypatch.setenv()` — no global side effects
    - YAML files are written to `tmp_path` — no fixtures needed beyond pytest builtins
    - `test_load_config_defaults` cleans relevant env vars first to ensure deterministic defaults

---

- [ ] **T-07.02 — Write test_policy/test_loader.py (6 tests)**
  - **File:** `tests/test_policy/test_loader.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `load_policy`, `load_policies` from `cce.policy.loader`
    - Import types from `cce.policy.types`
    - Use `tmp_path` for YAML files
    - Tests are mix of `@pytest.mark.unit` and `@pytest.mark.integration`
    - `test_load_policy_minimal` — YAML with `id: test`, `name: Test`. Verify defaults: `domains_allow == []`, `domains_deny == []`, `max_sources_per_run == 50`, `reputation` and `recency` are default instances
    - `test_load_policy_full` — YAML with all fields populated: `domains_allow`, `domains_deny`, `reputation` (with `trusted_institutions`), `recency` (with `max_age_days`), `max_sources_per_run`, `topic_overrides`. Verify complete parsing
    - `test_load_policies_directory` — create 2 YAML files in `tmp_path`. `load_policies(tmp_path)` returns dict with 2 entries keyed by policy `id`
    - `test_load_policies_skips_invalid` — directory with 1 valid and 1 malformed YAML (e.g., `": invalid yaml"`). Valid one loads; function doesn't crash
    - `test_parse_policy_topic_overrides` — YAML with `topic_overrides` containing `topic_pattern`, `domains_allow`, `reputation` override. Verify `TopicOverride` object is correctly constructed
    - `test_load_real_peer_reviewed_policy` (`@pytest.mark.integration`) — load actual `policies/peer-reviewed.yaml` from project root. Verify `id == "peer-reviewed"`, deny list includes `"reddit.com"`, trusted_institutions includes `"nih.gov"`, `max_sources_per_run == 15`
  - **Acceptance:**
    - `uv run pytest tests/test_policy/test_loader.py` passes all 6 tests
    - `test_load_real_peer_reviewed_policy` reads the actual project policy file (integration test, may need path adjustment)
    - YAML test files written to `tmp_path`
    - `test_load_policies_skips_invalid` does not raise an exception
