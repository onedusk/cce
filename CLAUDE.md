# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Content Curation Engine (CCE) — an evidence-first content pipeline that discovers sources, extracts verbatim evidence with provenance, synthesizes citation-backed drafts, and verifies every claim before publishing. Core invariant: no citation, no ship.

**Phase status:**

| Phase | Status | Reference |
|-------|--------|-----------|
| Phase 1 — core loop | Shipped | `orchestrator/pipeline.py` |
| Phase 2 — taxonomy tagging + embedding ranking | Shipped | `tagging/`, `discovery/discoverer.py` (embedding path) |
| Phase 3 — REST API + CLI | Shipped | `api/`, `cli.py`, `engine.py` |
| Phase 4 — platform integration | Not started | — |

## Commands

```bash
# Sync all deps (creates .venv automatically)
uv sync --all-extras

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/test_discovery/test_discoverer.py

# Lint
uv run ruff check src/

# Lint with auto-fix
uv run ruff check --fix src/

# Format
uv run ruff format src/

# Add a dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>
```

## Architecture

The pipeline flows: `CurationRequest → SourcePolicy → Discoverer → EvidenceStore → Writer → Verifier → QualityGate → PublishPackage`

**Key design constraints:**
- Writer produces drafts *only* from stored evidence objects — no training-data hallucination
- Verifier is a separate role that checks every claim against evidence
- Quality gate routes to PASS (publish), FAIL (rewrite loop), or REVIEW (human)
- Writer-verifier loop iterates up to max iterations per risk profile (2–4)
- **Humanization** (opt-in via `EngineConfig.humanization.enabled`, default off): programmatic style scorer + Editor agent + implied-claim checker sit between writer and verifier. Marker lists (suppressed vocabulary, hedging phrases, formulaic transitions, contrastive-frame regex) live in `config/humanization_markers.yaml` — operator-editable, updated without code changes to track the AI-marker coevolution problem. Scores ride on `ContentUnit.style_scores` (separate field from `ContentScores` — ADR-004) and are logged per iteration as `JobStage.SCORE` records for threshold calibration. Editor (M03) fires conditionally on `humanization_pass=False` and emits a `JobStage.EDIT` record; it must preserve every `[ev:ID]` marker — drift triggers a fallback to the writer's draft. Citation invariant (`gate.evaluate`) is unaffected by style scores in v1 (ADR-006 soft gate); the editor never extends `max_writer_iterations` (ADR-005). See `docs/decompose/humanization/` for full design.

**Abstractions use `typing.Protocol`**, not ABC:
- `LLMProvider` (`llm/base.py`) — implemented by `AnthropicProvider`
- `CrawlAdapter` (`discovery/adapters/base.py`) — implemented by `FirecrawlAdapter`
- `EvidenceStore` (`evidence/store.py`) — implemented by `SQLiteEvidenceStore`

**Dependency injection via constructors** — all components receive deps as args, no globals.

**Dependency flow** (no circular deps):
- `config/` and `models/` are the two roots with no dependencies
- `policy/` ← models
- `discovery/` ← models, policy, config, adapters
- `evidence/` ← models, config
- `tagging/` ← models, config (taxonomy-driven tagger consumed by discoverer + writer)
- `synthesis/` ← models, evidence, llm, config, tagging (`synthesis/writer.py`, `synthesis/scoring.py` [humanization M02 — no LLM deps], `synthesis/editor.py` [humanization M03 — LLM-based stylistic rewrite, citation-preservation enforced via post-call check])
- `verification/` ← models, evidence, policy, llm, config
- `orchestrator/` ← all pipeline modules
- `output/` ← models, orchestrator (consumed by CLI `emit-mdx` and runner scripts)
- `api/` ← orchestrator, models, config
- `engine.py` ← api, orchestrator, config (job lifecycle + mode dispatch; see the `CurationEngine` docstring)

**`engine.py` vs `orchestrator/pipeline.py`:** `engine.py` owns the job lifecycle — dispatching between embedded (in-process `Pipeline`) and remote (HTTP client) modes — while `orchestrator/pipeline.py` is pure stage orchestration consumed by the embedded mode. Treat them as separate roles; do not mix concerns.

## Conventions

- **src layout**: package lives at `src/cce/`, tests at `tests/`
- **All data models are frozen Pydantic BaseModels** in `models/` — pipeline modules import from there, never define shared types themselves
- **Adapter protocols live next to their consumers**, not in a separate `interfaces/` package
- **No `utils/` or `common/`** — shared data goes in `models/`, cross-cutting concerns at package root
- **Async throughout** — all I/O (LLM, crawl, storage) is async; pytest uses `asyncio_mode = "auto"`
- **Python ≥ 3.11**, managed with uv, linted with ruff, built with hatchling

## Environment

Requires `ANTHROPIC_API_KEY` and `FIRECRAWL_API_KEY` in `.env` (gitignored).

<!-- decompose:start -->
## Decompose Code Intelligence

This project has a decompose MCP server with code intelligence tools powered by tree-sitter and a graph database. For code understanding tasks, these tools provide richer context than manual file operations:

- `mcp__decompose__build_graph` — index the codebase (run once per session, persists to .decompose/graph/)
- `mcp__decompose__query_symbols` — find functions, types, interfaces by name
- `mcp__decompose__get_dependencies` — trace upstream/downstream dependencies
- `mcp__decompose__assess_impact` — compute blast radius of file changes
- `mcp__decompose__get_clusters` — discover tightly-coupled file groups

For the /decompose skill specifically:
- `mcp__decompose__get_stage_context` — load templates and prerequisite content
- `mcp__decompose__write_stage` — write stage files with validation and coherence checking
- `mcp__decompose__get_status` — check decomposition progress
<!-- decompose:end -->
