# Changelog

All notable changes to the Content Curation Engine (CCE).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Calibration script `scripts/run_score_sweep.py` (pure Python, $0 cost).

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
