# Configuration

How the engine is configured, from where, and in what order. This is the
authority document for configuration; `.env.example` is the quick reference.

## Precedence

For engine tuning values, three layers apply. Higher wins:

1. **Environment variables** (`CCE_*`, plus `ANTHROPIC_API_KEY` /
   `ANTHROPIC_MODEL` / `FIRECRAWL_API_KEY` aliases)
2. **YAML config file** — optional, passed explicitly (see below)
3. **Built-in defaults** — `src/cce/config/types.py` field defaults. The
   loader passes only explicitly-configured values, so `types.py` is the
   single source of defaults (audit-2026-06-09 finding 1.4)

A `.env` file at the repo root (gitignored) is the conventional place for
layer 1 — `cp .env.example .env` and edit. The CLI and engine read it via
`cce.load_env_file()`-style helpers and plain process environment; the loader
itself only consults `os.environ`. Note the `.env` parser splits on the first
`=` and does **not** strip inline comments, so keep comments on their own line.

## Passing a YAML config file

The engine config file is *opt-in* — nothing is loaded implicitly. Pass it:

- `uv run cce api start --config path/to/config.yaml`
- `uv run cce emit-mdx --config path/to/config.yaml ...` (and the
  `cce api key ...` commands accept `--config` the same way)
- `CurationEngine.embedded(config_path="path/to/config.yaml")` from code
- `load_config("path/to/config.yaml")` directly

Top-level YAML sections mirror `EngineConfig`: `llm`, `evidence_store`,
`crawl`, `embedding`, `quality_gate`, `api`, `humanization`, `engine_version`.
`config/humanization_live.yaml` is a working example (the humanization live
harness uses it). Environment variables override whatever the file says.

## How loading works

`ConfigRegistry.load(root, config_path)` (`src/cce/config/registry.py`) is
the one sanctioned load entry for engine and API code (ADR-002,
audit-2026-06-09). Both `CurationEngine.embedded()` and the API lifespan
build exactly one registry at startup and pass it to the component factory —
neither calls the individual loaders or hardcodes paths (a source-inspection
test in `tests/test_components.py` enforces this).

`load()` runs in this order:

1. **Engine config** — `load_config(config_path)` (env > YAML > `types.py`
   defaults). The API lifespan passes its already-built `EngineConfig` via
   the `engine=` keyword instead, since `create_app()` accepts one.
2. **Policies** — `load_policies(root / "policies")` (or the `policies_dir`
   argument). Forgiving per PDR-003: a missing directory yields an empty
   dict, a loader exception is logged and tolerated, and malformed
   individual files are already skipped inside `load_policies`. Pre-deploy
   strictness is `cce validate`'s job.
3. **Path configs** — the explicit `path_configs_path` argument when given;
   otherwise the first of `root / "path_configs" / thnklabs.yaml` (operator
   file, untracked) then `default.yaml` (committed template) that exists and
   yields a non-empty dict.
4. **Taxonomy** — records `root / "taxonomies" / "wellbeing-8d.yaml"` (or
   under the `taxonomies_dir` argument) as `taxonomy_path` when the file
   exists; parsing and plugin construction stay in
   `cce.components.build_components`.
5. **Markers** — `load_markers(root / humanization.markers_path)` only when
   `humanization.enabled` is true. A missing markers file raises — the one
   intentional fail-fast surface (an operator who enabled humanization must
   not silently ship unscored drafts).

`CurationEngine.embedded()`'s `policies_dir` / `taxonomies_dir` /
`path_configs_path` parameters feed these arguments directly (honored since
audit-2026-06-09 M06; they were accepted but ignored from Phase 3 until
then). Relative arguments resolve against `root`; absolute ones are used
as-is.

**New configuration surfaces must enter through the registry** — add a field
to `ConfigRegistry`, load it in `load()`, and consume it from
`build_components`. Do not add `load_*` calls to `engine.py` or
`api/app.py`; the drift-tripwire test will fail the build.

## YAML directories: content vs engine tuning

Four directories hold YAML, with two distinct roles:

**Content configuration** — describes *what* to curate and *how output is
shaped*. Loaded by id at job time, not part of `EngineConfig`:

- `policies/` — `SourcePolicy` definitions (domain allow/deny, reputation
  tiers, recency rules). Keyed by the `id` field inside each file — that id is
  what `--policy-id` and the API's `policy_id` refer to. The API loads every
  `*.yaml` in this directory at boot; malformed files are logged and skipped
  (boot resilience — see PDR-003 in the audit pack).
- `taxonomies/` — taxonomy definitions for evidence classification. The API
  currently selects `taxonomies/wellbeing-8d.yaml` when present.
- `path_configs/` — output path definitions (tone, structure, depth per
  path). The API tries an operator-supplied file first (untracked), then
  falls back to the committed `path_configs/default.yaml`.

**Engine tuning** — describes *how the engine runs*:

- `config/` — the optional engine config YAML you pass with `--config`
  (e.g. `config/humanization_live.yaml`), plus
  `config/humanization_markers.yaml`: operator-editable marker lists for the
  humanization scorer, located via `CCE_HUMANIZATION_MARKERS_PATH`.

## Environment variables

The complete inventory, grouped as in `.env.example`. Defaults shown are the
effective values when neither env var nor YAML provides one.

### Required secrets

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | LLM provider key (writer, verifier, editor) |
| `FIRECRAWL_API_KEY` | Crawl adapter key (source discovery) |

### LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_LLM_PROVIDER` | `anthropic` | Provider id (only `anthropic` implemented) |
| `CCE_LLM_MODEL` | `claude-sonnet-4-6` | Model id |
| `ANTHROPIC_MODEL` | — | Fallback alias for `CCE_LLM_MODEL` |
| `CCE_LLM_API_KEY` | — | Overrides `ANTHROPIC_API_KEY` when set |
| `CCE_LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `CCE_LLM_MAX_TOKENS` | `4096` | Per-call output token cap |

### Evidence store

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_EVIDENCE_BACKEND` | `sqlite` | Backend id (only `sqlite` implemented) |
| `CCE_EVIDENCE_SQLITE_PATH` | `evidence.db` | SQLite file (also jobs + API keys) |

### Crawl

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_CRAWL_ADAPTER` | `firecrawl` | Adapter id (only `firecrawl` implemented) |
| `CCE_CRAWL_API_KEY` | — | Overrides `FIRECRAWL_API_KEY` when set |
| `CCE_CRAWL_RATE_LIMIT` | `2.0` | Max crawl requests per second |
| `CCE_CRAWL_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `CCE_CRAWL_MAX_PER_SOURCE` | `5` | Max excerpts kept per source |
| `CCE_CRAWL_MAX_EVIDENCE` | `100` | Max evidence objects per request |

### Embedding (Ollama)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_EMBEDDING_ENABLED` | `true` | `false` = keyword-ranking fallback |
| `CCE_EMBEDDING_PROVIDER` | `ollama` | Provider id (only `ollama` implemented) |
| `CCE_EMBEDDING_MODEL` | `nomic-embed-text-v2-moe` | Model Ollama must serve |
| `CCE_EMBEDDING_DIMENSIONS` | `768` | Vector size |
| `CCE_EMBEDDING_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `CCE_EMBEDDING_TIMEOUT` | `30` | Per-batch timeout (seconds) |
| `CCE_EMBEDDING_BATCH_SIZE` | `64` | Texts per embedding request |

### API server

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_API_HOST` | `0.0.0.0` | Bind address for `cce api start` |
| `CCE_API_PORT` | `8000` | Bind port |
| `CCE_API_REQUIRE_AUTH` | `true` | `false` disables bearer auth (dev only) |
| `CCE_API_CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `CCE_API_MAX_CONCURRENT_JOBS` | `2` | Parallel pipeline jobs |

### Humanization

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_HUMANIZATION_ENABLED` | `false` | Master switch for scorer/editor/checker |
| `CCE_HUMANIZATION_MARKERS_PATH` | `config/humanization_markers.yaml` | Marker lists |

Granular humanization thresholds are deliberately YAML-only (reviewable in
diffs); env vars exist only for the master switch and the marker path.

### Logging

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_LOG_FORMAT` | unset | `json` switches to structured JSON logs |

## Ollama and embedding ranking

Discovery ranks crawled evidence semantically: excerpts are embedded via a
local [Ollama](https://ollama.com) server and scored against the topic with
cosine similarity (vectors persist in SQLite via sqlite-vec). This is
**enabled by default** because it is a quality default worth defending
(PDR-002 in the audit design pack) — but it means a fresh install has a local
dependency the API keys don't cover.

Setup:

1. Install Ollama: <https://ollama.com/download> (or `brew install ollama`)
2. Start the server: `ollama serve` (default address `http://localhost:11434`)
3. Pull the model: `ollama pull nomic-embed-text-v2-moe`

If the server lives elsewhere, set `CCE_EMBEDDING_BASE_URL`. If you change
the model, keep `CCE_EMBEDDING_MODEL` and `CCE_EMBEDDING_DIMENSIONS` in sync
with what the model actually emits.

**Failure mode today:** if Ollama is not reachable you will see a connection
error (`[Errno 61] Connection refused`) or an "Embedding provider
unavailable" warning at startup, and ranking quality degrades. If you don't
want to run Ollama at all, set `CCE_EMBEDDING_ENABLED=false` — discovery
falls back to keyword ranking and no embedding calls are made.
