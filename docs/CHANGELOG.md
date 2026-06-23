# Changelog

All notable changes to the Content Curation Engine (CCE).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — content-revision (client editorial feedback)

Engine remediation of the thnkLabs client editorial feedback
(`docs/internal/thnklabs-content-revision-plan-2026-06-18.md`, local-only),
decomposed under `docs/decompose/content-revision/` (local-only) and
implemented as milestone commits **M01–M04** on `feature/content-revision`. **M05**
(corpus regeneration + `emit-mdx` to the thnkLabs site) is an operational/e2e
step and is intentionally not part of these commits — it needs live API keys
and the corrected local `thnklabs.yaml`. Suite: 797 → **835 passed**;
coverage 94.8%.

### Changed — editorial structure (M01)
- **`path_configs/thnklabs.yaml`** (operator config, gitignored `*thnk*` — the
  change ships to the operator environment, not to main): LEARN
  `section_requirements`/`prompt_addendum` no longer carry the eight-dimensions
  framing or the `overview`/`closing_frame` scaffolding sections; EXPLORE is now
  the home of the eight-dimensions framing + curated resources; APPLY assumes
  Learn+Explore already read. (Client: each path should have a distinct mandate.)
- **`WRITER_SYSTEM_PROMPT`** (`synthesis/writer.py`) gains a `STRUCTURE GUIDANCE`
  block banning meta-introductions ("In this essay…") and labelled scaffolding
  headings ("Overview", "Closing Frame", "Conclusion", …). PDR-001, ADR-004.

### Fixed — citation de-duplication (M02)
- **`build_citation_index`** (`output/mdx/citations.py`) now keys footnote
  de-dup on the canonical source URL instead of `evidence_id`: a source cited
  via multiple evidence excerpts gets **one** footnote number per article
  (client finding: the same resource was listed under several numbers).
  Emit-time only — `ContentUnit.citations`/`evidence_map` keep full per-evidence
  granularity, so the "no citation, no ship" invariant is untouched. New
  `_canonical_url` strips the fragment + trailing slash (query strings
  preserved). ADR-001/002, PDR-003.

### Changed — cross-article de-duplication (M03)
- The three paths now generate **sequentially** (learn → explore → apply)
  instead of concurrently; each later path receives a digest of its siblings'
  claims and is instructed not to re-explain them. De-dup is **prose-level
  only** — a later path may and should re-cite shared sources. Replaces the
  `asyncio.TaskGroup` fan-out with a serial loop; adds
  `Writer.write(sibling_context=…)` and `_build_sibling_digest`. ADR-003/006,
  PDR-002.
- **Token budget now accumulates across paths** (ADR-003 "all paths"
  semantics): under sequential execution a later path's checkpoint sees earlier
  paths' spend. New regression test
  `test_budget_accumulates_across_paths_sequentially`.
- Gate attribution unchanged (already keyed by `gate_results_by_path`,
  T-07.05); the now-vestigial `BaseExceptionGroup` unwrap in `run()` removed;
  `test_pipeline_parallel_paths.py` → `test_pipeline_sequential_paths.py`
  (stale-name standard).

### Added — acceptance harness (M04)
- **`scripts/research/run_acceptance_check.py`** — deterministic structural
  checks (no scaffolding headings; dimensions-in-EXPLORE; one citation per URL)
  plus a semantic repetition check: an LLM-judge (authoritative, temp 0) and an
  embedding near-duplicate signal (reuses `EmbeddingProvider` +
  `_cosine_similarity`; sim_threshold 0.85). A lexical shingle overlap is a
  verbatim-copy tripwire only — lexical-overlap-as-gate was **empirically
  rejected** (ADR-007): the client's corrected trio scores *higher* shingle
  overlap than the bad engine output (shorter text + reworded repetition).

### Not in scope (this branch)
- **M05** — corpus regeneration + `emit-mdx --target` to the thnkLabs site:
  operational/e2e, run separately in an environment with the corrected local
  `thnklabs.yaml` present (else it regenerates the old structure).
- Unified cross-article bibliography (per-article chosen, ADR-002); ingesting
  the hand-edited `.pages` (references only, ADR-005); the LLM-judge live
  calibration (deferred to the M05 environment).

## [0.3.0] — 2026-06-10

Full remediation sprint from the 2026-06-09 codebase audit
(`docs/internal/improvement-opportunities-2026-06-09.md`, local-only),
implemented as 8 ordered milestone commits plus 2 review-fix commits,
merged via PR #2. Minor bump: additive API schema change
(`JobResponse.request`), new CLI commands and config surface, plus
operator-facing validation tightening (noted below).
Suite: 695 → **797 passed**; coverage measurement switched from statement
to branch (floor 90 → 92, observed 94.71%).

### Fixed
- **Remote mode was broken in production** (`b815692`): `JobHandle.status()` /
  `wait()` / `retry()` failed `Job.model_validate` because the API's
  `JobResponse` lacked the `request` field. Found by the first-ever
  remote-mode tests (audit finding 3.1 predicted exactly this blind spot).
  Fix is additive and wire-compatible; `docs/openapi.json` regenerated
  (`813e70a`).
- Evidence search route double `model_dump` removed — the last remainder of
  prior-audit finding 1.2 (`060328e`).
- Startup config errors render one actionable line instead of a
  Starlette-formatted traceback: lifespan catches `ConfigError` →
  `SystemExit(1)`; markers `FileNotFoundError` wrapped as `ConfigError` in
  `ConfigRegistry.load`; missing API keys surface before optional-surface
  errors in `embedded()` (`899cae3`, `813e70a`).
- Latent test flakes: wall-clock upper-bound assertion replaced with a
  concurrency high-water-mark counter; 50 ms job-completion sleeps replaced
  by poll-with-deadline; `hash_prefix` NameError guard (`fb2f0f3`).

### Added — operator workflows (M08, `2566e3b`)
- **`cce curate <topic>`** — single-topic submission via the embedded engine;
  exits 0 COMPLETED / 2 REVIEW_REQUIRED / 1 FAILED-or-config-error.
- **`cce status <job_id>`** / **`cce jobs`** — inspect job state, stage
  metrics (incl. token usage and budget notes), and gate outcomes straight
  from the job store; no API server required (finding 4.2).
- **`cce validate`** — strict YAML checking for `policies/`,
  `path_configs/`, `taxonomies/` with `difflib` did-you-mean suggestions on
  unknown keys; exit 1 on any error (finding 4.7, PDR-003: load-time stays
  forgiving, validate is the strict moment).
- **`EngineConfig.max_tokens_per_job`** (`CCE_MAX_TOKENS_PER_JOB`) — per-job
  LLM token ceiling checked at writer-iteration boundaries; on breach the
  path stops iterating, the gate feedback carries a budget note, and the job
  routes to REVIEW_REQUIRED keeping partial drafts (finding 2.1, ADR-003).
  Worst-case overshoot: one writer+verifier pair per in-flight path.

### Added — fail-fast config + API hardening (M01, `060328e`)
- `ConfigError` + `validate_required_keys` — missing `ANTHROPIC_API_KEY` /
  `FIRECRAWL_API_KEY` now fails in one line at every pipeline entry point
  (CLI, embedded engine, API lifespan); keyless commands (`emit-mdx`,
  `api key generate`) are untouched (finding 4.3, ADR-006).
- Request body-size limit middleware: `Content-Length` > 1 MiB → 413
  envelope with `request_id` (finding 5.1). Known limitation: chunked bodies
  bypass the check (bounded downstream by uvicorn/h11).
- **Operator-facing tightening:** `SourcePolicy` and its nested rule models
  now reject unknown YAML keys (`extra="forbid"`, finding 6.3) and
  `CurationRequest.subtopics` elements are capped at 200 chars (finding
  5.2). All repo YAML verified to still parse; a typo'd key in a policy file
  now fails loudly via `cce validate` instead of silently doing nothing.

### Added — structural (M05/M06, `9b2d200`/`15bf42e`)
- **`cce/components.py`** — `ComponentSet` + `build_components` +
  `build_pipeline`: the single wiring authority consumed by both embedded
  and API modes (finding 1.1, ADR-001). Fixes a layering violation:
  `embedded()` previously late-imported `api/app._build_pipeline`. Parity,
  fallback-semantics, and field-completeness tests pin the contract.
- **`config/registry.py`** — `ConfigRegistry.load()` owns the full config
  sequence (engine config, policies, path-config selection, taxonomy path,
  markers) and its precedence (env > YAML > `types.py` defaults; finding
  1.3, ADR-002). `embedded()`'s `taxonomies_dir`/`path_configs_path`
  parameters are honored again — they had been silently dead since Phase 3.
  Policy loading unified on the forgiving path (PDR-003).
- Loader defaults dedup: `load_config()` passes only explicitly-present
  values; `types.py` field defaults are the single source (finding 1.4).

### Changed — pipeline internals (M07, `f05c454`; behavior-preserving, ADR-005)
- `Discoverer.discover()` returns **`DiscoveryResult`** (evidence + metrics);
  the mutable `last_discover_metrics` side-channel is deleted. `run()`
  (~230 → 115 lines) and `_write_verify_loop` (~278 → 136) decomposed into
  phase helpers with direct unit tests (finding 1.2).
- **`ContentUnit.draft_source`** (`"writer"` | `"editor"`, default
  `"writer"`) — the editor's citation-drift fallback is now visible to the
  verifier and package consumers; `with_scores()`/`with_style_scores()`
  replace the 9-field `model_copy` (finding 1.5). Stored packages parse
  unchanged via the default.
- Stage/gate grouping uses explicit `(path, iteration)` keys instead of
  list order (`StageRecord.path` added, additive); verifier `pass_rate`
  logs a warning when the LLM returns inconsistent counts instead of
  silently clamping; embedding batches log a timing line; an unreachable
  Ollama now produces an error naming the base URL and the
  `CCE_EMBEDDING_ENABLED=false` fallback (findings 1.5, 2.3, PDR-002).

### Tests & CI (M02/M04, `fb2f0f3`/`b815692`)
- **Tier-marker enforcement**: a conftest collection hook fails the run on
  any unmarked test; full backfill — `unit`/`integration`/`slow`/`e2e` now
  partition the suite exactly (finding 3.4). A key-gated e2e smoke test
  gives the registered `e2e` marker its first member (skips without keys).
- **Operational-shell coverage** (finding 3.1–3.3): remote mode end-to-end
  via `httpx.ASGITransport`, pipeline-crash → FAILED, the real lifespan
  shutdown handler (the test-local logic copy was deleted), the real
  `embedded()` factory (no private-attr pokes), `cce batch` happy path.
  `engine.py` 64% → 95%.
- **Branch coverage** enabled (`[tool.coverage.run] branch = true`); floor
  recalibrated 90 → 89 → 92 per ADR-004's two-step. `tests/` brought under
  ruff in CI and pre-commit. CI gains an **OpenAPI freshness gate** that
  regenerates `docs/openapi.json` and fails on drift (finding 4.8).

### Docs & onboarding (M03, `46f5ef8`)
- README quick start rewritten around the `cce` CLI (PDR-001); Ollama
  documented as on-by-default with the keyword-ranking fallback (PDR-002,
  finding 4.5); `.env.example` now covers all 31 env reads (was 6 of an
  estimated 41 — finding 4.4); new `docs/configuration.md` precedence guide.
- `scripts/README.md` classifies every script; research/calibration
  artifacts moved to `scripts/research/` (finding 4.6); `AGENTS.md` tracked
  and reconciled with CLAUDE.md; stale code comments citing a never-existent
  audit path repointed (audit §0.2).

### Not in scope (deliberate — audit Deferred list)
- Feedback-iteration evidence subsetting (prompt caching absorbs the cost);
  evidence tags index (await Phase 4 tag-based discovery); configurable
  writer/verifier temperatures; excerpt sanitization (Firecrawl returns
  markdown; revisit at Phase 4); formal API envelope versioning (pre-1.0
  stance documented); `CurationEngine` embedded/remote subclass split
  (deferred until a third mode exists).
- The forgiving policy loader still drops unknown top-level YAML keys
  (`_parse_policy` forwards known keys explicitly); strict checking is
  `cce validate`'s job by design (PDR-003).

## [0.2.0] — 2026-04-24

Minor bump: Phase B Layer 2 — contrastive-frame subtype tagging. Schema
change on `StyleScores`, `HumanizationMarkers`, `ContrastiveFrame`, and
the `ScoreMetrics` TypedDict. Backward-compatible YAML and backward-
compatible external consumers (the total count field is preserved).

### Added — Phase B Layer 2 (contrastive subtype architecture)
- **`HumanizationMarkers.contrastive_parasitic_patterns`** (`src/cce/config/markers.py`) — new field for parasitic regexes ("X is not A. It is B"). `contrastive_patterns` retained for genuine-alternative regexes. Additive; operator YAMLs that define only the old key still load.
- **`HumanizationMarkers.compiled_contrastive_patterns()`** now returns `list[tuple[Pattern, subtype]]` where subtype is `"parasitic"` or `"genuine_alternative"`. Consumers in `Scorer` and `ImpliedClaimChecker` updated.
- **`StyleScores.contrastive_parasitic_count`** + **`contrastive_alternative_count`** (`src/cce/models/style.py`). The existing `contrastive_frame_count` is preserved as the sum for backward compatibility; the threshold gate (`max_contrastive_frames_per_1000`) still runs against the total.
- **`ContrastiveFrame.kind`** (`src/cce/synthesis/implied_claims.py`) — `"parasitic"` | `"genuine_alternative"`. Default: `"genuine_alternative"` for existing callers.
- **`ScoreMetrics` TypedDict** (`src/cce/models/job.py`) carries the subtype split; `Pipeline` populates both fields on `JobStage.SCORE` records.
- **Production parasitic patterns** added to `config/humanization_markers.yaml` (period-split + comma-split "X is not A. It is B"). Promoted from the Phase B corpus census (46 matches, 0/46 confirmed false positives, 2/46 ambiguous — see 0.1.2 discussion and `output/parasitic_matches_review.md`).

### Changed — Editor behavior
- **`EDITOR_SYSTEM_PROMPT`** now has distinct directives for the two subtypes (`src/cce/synthesis/editor.py`):
  - Genuine-alternative: apply the spectrum principle (unchanged, now explicitly scoped).
  - Parasitic: collapse to the direct claim. Explicit anti-attribution guard ("Do not attribute to 'some argue'"). Ambiguous-case caveat preserves the "not A" half when it carries independent factual weight (covers the 4.3% ambiguous class from Diagnostic 1 — e.g., clinical-safety contrasts).
- **Per-call flag list** in `_build_user_prompt` surfaces the subtype split so the editor knows which directive applies.

### Changed — Implied-claim checker
- **`ImpliedClaimChecker.check()`** skips LLM topic extraction for parasitic frames (`src/cce/synthesis/implied_claims.py`). Parasitic frames have no dismissed topic to counter-search against; skipping saves one LLM request per parasitic frame and eliminates the "fragment too short" warnings the extractor logs on them.

### Tests
- `tests/test_config/test_humanization.py` — new `test_parasitic_patterns_tagged_and_match_reframe_construction`; existing `test_compiled_contrastive_patterns_match_known_ai_prose` updated for the tuple return shape.
- `tests/test_synthesis/test_scoring.py` — new `test_score_subtype_split_parasitic_vs_alternative` and `test_score_parasitic_only_body`.
- `tests/test_synthesis/test_implied_claims.py` — new `test_detect_frames_tags_parasitic_vs_genuine`, `test_check_skips_parasitic_frames_no_llm_call`, `test_check_still_processes_genuine_alternative_when_parasitic_present`.
- `tests/test_synthesis/test_editor.py` — new `test_editor_system_prompt_has_parasitic_directive`.
- Suite: **692 passed, 3 skipped** (was 685).

### Not in scope (deliberate)
- LLM-based classification token (`parasitic | factual | unsure` per frame). Diagnostic 1 showed 0/46 confirmed false positives on real corpus; the editor-prompt "preserve 'not A' when it carries independent factual weight" caveat covers the 2/46 ambiguous class at zero LLM cost.
- Per-subtype thresholds on `HumanizationThresholds`. Only the total `max_contrastive_frames_per_1000` gates today; subtype counts are informational until calibration data warrants separate thresholds.
- `EditorConfig.contrastive_strategy` research-IV hook. Deferred until the combined-layer verification run (next step) measures residual parasitic frequency.

## [0.1.2] — 2026-04-24

Opportunistic patch release. Bundles one hardening fix from the 2026-04-22
security review (previously dismissed as not-currently-exploitable), one
marker-list expansion grounded in corpus evidence, and one operator-config
addendum for the learn path. No API breaks.

### Security (hardening)
- **CORS — disable `allow_credentials` when `allow_origins` contains `"*"`** (`src/cce/api/app.py`). Starlette's `CORSMiddleware` otherwise reflects the inbound `Origin` header + emits `Access-Control-Allow-Credentials: true`, defeating the browser's wildcard-vs-credentials safety rule. Bearer-header auth doesn't travel on cross-origin fetches today (the 2026-04-22 review dismissed this as a standalone finding), but closes the door for any future cookie-auth or session surface. Regression test covers both branches (`tests/test_api/test_cors.py`).

### Humanization
- **Add `\bby contrast\b` to `config/humanization_markers.yaml` `contrastive_patterns`**. The corpus census (`scripts/research/run_contrastive_census.py`, 2026-04-22) found 14 real genuine-alternative matches uncaught by the existing four patterns. No subtype tagging yet — the tagged-structure refactor is scoped for Phase B.
- **Operator config: `path_configs/thnklabs.yaml` learn `prompt_addendum`** — added a 3-sentence directive to avoid the "X is not A. It is B" reframe pattern when B restates or expands X. Empirically validated by a 3-topic test run (curiosity/boredom/stress): parasitic frame count dropped from 20 → 12 (-40%) with the addendum; learn-path max dropped from 10 to 6. See `output/parasitic_matches_review.md` (local/gitignored). This file is gitignored as client-specific; the change ships to the operator environment, not to main.

### Changed
- Test suite: **685 passed, 3 skipped** (was 683 — +2 CORS regression cases).

## [0.1.1] — 2026-04-22

Patch release: closes a HIGH-severity authentication gap on jobs read
routes discovered in a same-day security review.

### Security
- **Protect jobs read routes** (`get_job`, `list_jobs`, `get_package` in `src/cce/api/routes/jobs.py`). Before this release, three GET handlers on the jobs router were reachable unauthenticated while every other sensitive route was authed. An unauthenticated client could enumerate all job ids via `GET /v1/curate/jobs` and exfiltrate full `PublishPackage` content (draft text, evidence references with source URLs, verification report) via `GET /v1/curate/jobs/{id}/package` — the same data the authed evidence endpoints defend.
- **Breaking change.** Clients polling job status without a bearer token will now receive `401`. Pass `Authorization: Bearer <key>` on all jobs + evidence requests.

### Changed
- Auth is now attached at the router level via `app.include_router(jobs_router, dependencies=[Depends(auth_dependency)])` (same for `evidence_router`). Per-route `_auth: str | None = Depends(auth_dependency)` params were dropped as redundant on `create_job`, `delete_job`, `retry_job`, `get_evidence`, `search_evidence`. The meta router (`/v1/health`, `/v1/meta`) remains intentionally unauthed.
- `tests/test_api/test_auth_parameterized.py`: `PROTECTED_ROUTES` grew to 8 entries; `UNPROTECTED_ROUTES_EXPECTED` reduced to health + meta only. The file's explicit-inventory philosophy was preserved (no auto-enumeration of `app.routes`).

### Dismissed (recorded, not acted on)
- CORS `allow_credentials=True` with default `allow_origins=["*"]` was identified in the same review but dismissed as a standalone finding. The API uses bearer tokens in the `Authorization` header only (no cookies), so browsers don't auto-attach credentials on cross-origin fetches. Once the auth gap above is closed, the CORS misconfiguration has no exploitable credentialed surface. It remains a hardening improvement should the API ever gain cookie-based auth.

## [0.1.0] — 2026-04-22

Initial release. Phases 1-3 shipped end-to-end: an evidence-first pipeline
that discovers sources, extracts verbatim evidence with provenance, synthesizes
path-aware drafts, humanizes them (opt-in), verifies every claim, and exposes
the lifecycle over a REST API and CLI. Validated across live runs on 15+
topics; the "no citation, no ship" invariant is enforced by the quality gate.

### Added — Phase 1: Core loop
- `Pipeline` orchestrator wiring `CurationRequest → Discoverer → EvidenceStore → Writer → Verifier → QualityGate → PublishPackage`.
- Frozen Pydantic v2 data contracts in `src/cce/models/` (shared across all modules).
- `LLMProvider`, `CrawlAdapter`, and `EvidenceStore` as `typing.Protocol` abstractions; `AnthropicProvider`, `FirecrawlAdapter`, and `SQLiteEvidenceStore` as the initial implementations.
- Writer-verifier loop with per-path iteration caps tied to the risk profile (2-4).
- Quality-gate routing: PASS / FAIL (rewrite) / REVIEW (human).
- Source policy enforcement at discovery: recency, reputation, COI, jurisdiction.
- Evidence capping + cross-topic validation.

### Added — Phase 2: Ranking, tagging, policy
- Semantic evidence ranking via Ollama (`nomic-embed-text-v2-moe`) + `sqlite-vec` — shipped 2026-03-25.
- Rules-based taxonomy tagger with YAML taxonomy definitions (8-dimension wellbeing default).
- Path-aware writer modulation (tone/structure/depth per `learn`/`explore`/`apply`).
- Verifier trust weighting by source reputation.
- Per-path tuning fields: `max_evidence`, `max_paragraphs`, `subtopic_limit`.
- Domain policy templates under `policies/`.
- Pre-crawl URL dedup + evidence rehydration via `EvidenceStore.get_existing_urls`.
- Crawl failure tracking + taxonomy degradation signaling.

### Added — Phase 3: API + CLI + MDX
- FastAPI REST layer (`src/cce/api/`) with typed `response_model` annotations and generated OpenAPI spec (`docs/openapi.json`).
- `CurationEngine` facade with embedded/remote mode dispatch (`engine.py`).
- `cce` CLI: `run`, `batch`, `emit-mdx`, `api key generate`.
- Post-hoc MDX export (`src/cce/output/mdx.py`) with `--dry-run` / `--verbose`.
- Structured API envelope with `code` + `message` + `request_id`.
- `RequestIdMiddleware` + contextvar for request-scoped logging.
- Uniform auth across every protected route; `0600`-mode key files by default.
- Graceful shutdown with timeout + orphan job cleanup.
- Job-scoped logging + LLM token tracking; per-stage `StageRecord.metrics` with TypedDict schemas.

### Added — Humanization (opt-in, default off)
- `HumanizationConfig` master switch + `HumanizationThresholds` (calibrated 2026-04-17 against 36 archival drafts).
- **M02 — Programmatic style scorer** (`synthesis/scoring.py`, no LLM deps): 7 metrics — sentence-length stddev, type-token ratio, suppressed vocabulary, formulaic transitions, contrastive frames, hedging density, em-dash density.
- **M03 — Editor agent** (`synthesis/editor.py`): LLM-based stylistic rewrite, enforces citation preservation post-call; falls back to writer draft if any `[ev:ID]` marker drifts.
- **M04 — Implied-claim checker** (`synthesis/implied_claims.py`): flags contrastive frames ("Unlike X, Y") that dismiss topics with counter-evidence, with a release valve for well-cited dismissals.
- Marker lists in `config/humanization_markers.yaml` — operator-editable, reloadable without code changes (tracks the AI-marker coevolution problem).
- Per-iteration `JobStage.SCORE` and `JobStage.EDIT` records for threshold calibration.
- Reference config at `config/humanization_live.yaml`.
- Calibration script `scripts/research/run_score_sweep.py` (pure Python, $0 cost).

### Added — Infrastructure
- Prompt caching with cache-token accumulation across writer/verifier/editor/implied-claim calls.
- Concurrent per-path writer/verifier loops via `asyncio.gather` + `asyncio.TaskGroup` (sibling cancellation on first exception).
- Concurrent embedding batch dispatch with a capped semaphore.
- Process-global Firecrawl semaphore keyed on `(api_key, base_url)`; RPS warning on divergent configs.
- Optional JSON log formatter via `CCE_LOG_FORMAT=json`.
- API request logging middleware.
- `pytest-cov` + 90% coverage floor (raised from 70% in audit T1).
- `pyright` type checking wired into dev deps.
- `.pre-commit-config.yaml` with `ruff` + `ruff-format`.

### Changed
- `engine.py` owns job lifecycle and mode dispatch; `orchestrator/pipeline.py` is pure stage orchestration (separation documented in ADR-005).
- Quality-gate profiles are single-sourced in `config.types.QUALITY_GATE_PROFILES`.
- Writer and verifier share a single pre-formatted evidence block per path.
- YAML loader error semantics unified across policy / taxonomy / path configs (ADR-006).
- `lru_cache(maxsize=32)` on path-keyed YAML loaders.
- `VerificationReport.pass_rate` clamped to `[0, 1]`.
- `run_*.py` runners moved to `scripts/` with paths anchored to `ROOT = Path(__file__).resolve().parent.parent`.

### Fixed
- `[ev:HASH]` citation resolver handles writer outputs that drop the `ev_` prefix.
- MDX emitter strips citation gaps from editor output.
- Em-dash metric: writers over-use em dashes; threshold tuned as an editorial target, not engine floor.
- `policy.max_sources_per_run` applies to fresh + reusable evidence combined.
- Missing path groups padded with `FAIL` in `_terminal_decisions` so the terminal summary is complete.
- `published_date` handles the list-form Firecrawl returns for some domains.
- Input validation, embedding-batch chunking, frozen request models.
- LLM retry hardened with jitter + explicit `JSONDecodeError` handling.
- Batch inserts used for evidence writes; double `model_dump` call removed.

### Dev standards
- src layout: package at `src/cce/`, tests at `tests/`.
- All data models are frozen Pydantic `BaseModel`s in `models/`; pipeline modules import from there.
- Adapter protocols live next to their consumers, not in a separate `interfaces/` package.
- No `utils/` or `common/`.
- Async throughout; `pytest` with `asyncio_mode = "auto"`.
- Python ≥ 3.11, managed with `uv`, linted with `ruff`, built with `hatchling`.
