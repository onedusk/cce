# Repository Guidelines

## Project Structure & Module Organization
`src/cce/` contains the application code. Keep feature work inside the existing domain packages such as `api/`, `config/`, `discovery/`, `evidence/`, `synthesis/`, `verification/`, and `orchestrator/`. Shared contracts live in `src/cce/models/`.

`tests/` mirrors the package layout (`tests/test_api/`, `tests/test_orchestrator/`, etc.). Operational scripts live in `scripts/`. YAML-driven behavior belongs in `policies/`, `taxonomies/`, `path_configs/`, and `config/`. Treat `output/`, `logs/`, and `*.db` files as generated runtime state, not source.

## Build, Test, and Development Commands
- `uv sync --all-extras` installs runtime and dev dependencies.
- `uv build` creates sdist and wheel artifacts.
- `uv run ruff check src/ tests/` runs lint checks; `uv run ruff format src/ tests/` formats code.
- `uv run pyright` runs static type checks.
- `uv run pytest --tb=short -q` runs the full suite with coverage enforcement.
- `uv run cce batch --topics-file <file> --policy-id <id>` runs the pipeline (the supported front door; see `scripts/README.md` for the legacy hardcoded-config examples).
- `uv run cce api start --port 8000` starts the local API server.
- `pre-commit run --all-files` applies the same Ruff checks used before commit.

## Coding Style & Naming Conventions
Target Python 3.11. Use 4-space indentation, explicit type hints, and short docstrings where intent is not obvious. Let Ruff handle formatting and import ordering.

Use `snake_case` for modules, functions, files, and YAML IDs; use `PascalCase` for classes and Pydantic models. Follow the existing package split instead of adding cross-cutting modules.

## Testing Guidelines
The project uses `pytest`, `pytest-asyncio`, and `pytest-cov`. Name tests `test_<behavior>.py` and place them under the matching feature area. Every test must carry a tier marker — `unit`, `integration`, `slow`, or `e2e` (collection fails on unmarked tests); `e2e` tests require real `ANTHROPIC_API_KEY` and `FIRECRAWL_API_KEY` and skip without them.

The suite enforces the branch-coverage floor set in `pyproject.toml` (`--cov-fail-under`; the comment there is the source of truth). Any behavior change should include targeted tests plus a full `uv run pytest --tb=short -q` run before review.

## Commit & Pull Request Guidelines
Recent history uses concise prefixes such as `fix:`, `fix(scope):`, `docs:`, `chore:`, and `release:`. Keep commit subjects imperative and specific.

PRs should summarize the behavior change, list the verification commands you ran, and link the relevant issue, audit note, or task. Include sample CLI/API output when changing `api/`, auth, or generated content.

## Security & Configuration Tips
Start from `.env.example`; never commit real secrets or local `.env` changes. Generate API keys with `uv run cce api key generate`, and keep emitted key files private. Review edits to YAML policies, taxonomies, path configs, and `docs/openapi.json` carefully because they directly affect runtime behavior.
