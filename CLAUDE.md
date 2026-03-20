# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Content Curation Engine (CCE) — an evidence-first content pipeline that discovers sources, extracts verbatim evidence with provenance, synthesizes citation-backed drafts, and verifies every claim before publishing. Core invariant: no citation, no ship.

Phase 1 (core loop) is implemented. Phases 2–4 (tagging plugins, REST API, platform integration) are not started.

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
- `synthesis/` ← models, evidence, llm, config
- `verification/` ← models, evidence, policy, llm, config
- `orchestrator/` ← all pipeline modules
- `api/` ← orchestrator, models, config (Phase 3, not yet built)

## Conventions

- **src layout**: package lives at `src/cce/`, tests at `tests/`
- **All data models are frozen Pydantic BaseModels** in `models/` — pipeline modules import from there, never define shared types themselves
- **Adapter protocols live next to their consumers**, not in a separate `interfaces/` package
- **No `utils/` or `common/`** — shared data goes in `models/`, cross-cutting concerns at package root
- **Async throughout** — all I/O (LLM, crawl, storage) is async; pytest uses `asyncio_mode = "auto"`
- **Python ≥ 3.11**, managed with uv, linted with ruff, built with hatchling

## Environment

Requires `ANTHROPIC_API_KEY` and `FIRECRAWL_API_KEY` in `.env` (gitignored).
