# Changelog

All notable changes to the Content Curation Engine (CCE).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
