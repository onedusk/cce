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
layer 1 — `cp .env.example .env` and edit. The `cce` CLI loads `./.env` from
the working directory at start-up (variables already set in the environment
win); the runner scripts do the same via `cce.load_env_file()`. The embedded
engine and the library don't read `.env`: they only consult `os.environ`. Note the `.env` parser splits on the first
`=` and does **not** strip inline comments, so keep comments on their own line.

## Passing a YAML config file

The engine config file is *opt-in* — nothing is loaded implicitly. Pass it:

- `uv run cce api start --config path/to/config.yaml`
- `uv run cce emit-mdx --config path/to/config.yaml ...` (and the
  `cce api key ...` commands accept `--config` the same way)
- `CurationEngine.embedded(config_path="path/to/config.yaml")` from code
- `load_config("path/to/config.yaml")` directly

Top-level YAML sections mirror `EngineConfig`: `llm`, `writer`, `verifier`,
`evidence_store`, `crawl`, `embedding`, `quality_gate`, `api`, `humanization`,
`publish_policy`, `max_tokens_per_job`, `engine_version`. Quality-gate
profiles take `pass_threshold` (renamed from `autopublish_threshold`, which is
still accepted). `writer.temperature` (default `0.2`) and
`verifier.temperature` (default `0.1`) are the per-agent sampling
temperatures; like `llm.temperature` they are not sent to models that reject
sampling parameters (Opus 4.7 and later, Sonnet 5, Fable 5).
`config/humanization_live.yaml` is a working example (the humanization live
harness uses it). Environment variables override whatever the file says.

The file is checked strictly when it loads. A path that does not exist is a
`ConfigError` (relative paths resolve against the working directory), not a
silent fall-back to the defaults, and so is a key that no config model
defines, at any level (`publish_polcy`, `api.hosst`,
`humanization.editor.enabld`); the error names every such key. Code that
builds `EngineConfig` directly gets a pydantic `ValidationError` for an
unknown field. `embedding.concurrency` is read from YAML only (no env var).
The engine and the API log the effective `publish_policy` once at start-up.

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

**Injecting providers instead of configuring them.** A consumer that must
route outbound calls through its own gateway passes
`overrides=ComponentOverrides(llm=..., verifier_llm=..., crawl_adapter=...,
embedding=...)` to `CurationEngine.embedded()` or `build_pipeline()`
(`src/cce/components.py`). Each field left `None` is built from config as
usual; an injected `llm` also serves the editor and implied-claim checker,
and the verifier unless `verifier_llm` is given (setting `verifier.model`
with only `llm` injected raises). An injected LLM or crawl adapter needs no
`ANTHROPIC_API_KEY` / `FIRECRAWL_API_KEY`. Injected LLM providers must accept
`output_schema`, set `stop_reason`, and report the `input_tokens`,
`output_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`
usage keys (the token budget counts input, output and cache-creation
tokens; cache reads are not counted). Configuration itself still loads
only through the registry.

**Non-web sources.** To mix documents that aren't web pages into a run,
write a `CrawlAdapter` for them and combine it with the web adapter:
`CompositeCrawlAdapter({"local": my_adapter}, default=FirecrawlAdapter(config.crawl))`
(`src/cce/discovery/adapters/composite.py`), injected as
`ComponentOverrides(crawl_adapter=...)`. The composite dispatches each URL on
its scheme; a scheme with no adapter counts as a failed crawl. An adapter
that serves one tenant's documents must not sit in a `ComponentSet` other
tenants share (see the per-tenant note under Evidence store).

- **URL shape:** `scheme://host/path`, for example `local://acme/q3-report.pdf`
  or `gdrive://<file-id>`. The host part is required: the source policy
  matches `domains_allow` / `domains_deny` against it exactly as for a web
  domain, and drops a URL without one, so `file:///x.pdf` is never crawled.
  With a non-empty `domains_allow`, list the pseudo-host too (`acme`).
- **Finding documents:** the adapter's `search(query, limit)` returns the
  pseudo-URLs to consider. The composite asks every adapter (each for up to
  `limit`) and interleaves the answers, so `max_sources_per_run` can't cut
  one adapter out entirely.
- **What the adapter returns:** a `CrawlResult` with the document as
  markdown. Chunking, the 50-character minimum, the recency and reputation
  filters, dedup, storage and URL reuse then apply as for a web page; the
  URL is what citations and `_evidence.json` carry, so it should mean
  something to your readers or your renderer.

**Reading a failed run's error in memory.** In embedded mode,
`JobHandle.error` holds the exception that failed the job's last run (None
until then or when no exception failed it, as with no evidence; cleared by
`retry()`; always None in remote mode). For an
unreadable writer or verifier reply it is an `UnparseableResponseError`
whose `raw_response` is the reply text, for the caller to persist where it
sees fit. cce never logs or stores that text: the job store and the API only
carry `job.error` (code, message and stage).

**Retrying a job.** `JobHandle.retry()` and `POST /v1/curate/jobs/{id}/retry`
re-run a finished job under the same id. Before the job is queued again the
retry removes the previous run's package and stage records, so
`JobHandle.package()` returns None (the API answers 404 `package_not_found`)
until the new run stores one, and a retry that fails before any path
completes leaves the job FAILED with no package. A retry whose policy is no
longer loaded is refused (`ValueError` in embedded mode, 404
`policy_not_found` from the API) and the job is left as it was.

**Recovering a job left by a crash.** A QUEUED or RUNNING job is refused by
retry (409 `already_running`, `ValueError` in embedded mode). A graceful API
shutdown marks every RUNNING job in its store FAILED (`server_shutdown`),
including one a CLI process sharing the same file is running; after a kill,
OOM or host crash the row keeps its status with nothing running it, and cce
does not fail such jobs at start-up. To recover one, call
`POST /v1/curate/jobs/{id}/retry?force=true` (or `JobHandle.retry(force=True)`).
When the serving process runs no task for the job, it records the job FAILED
with error code `orphaned` and then re-queues it; when it does run one, the
answer is still 409. The `orphaned` failure is recorded before the policy is
checked: if the policy is no longer loaded the retry is still refused (404
`policy_not_found`, `ValueError` in embedded mode), and the job stays FAILED,
retryable without force once the policy is back. In embedded mode
`JobHandle.cancel()` on such a job
records the same `orphaned` failure without re-running it (the API's DELETE
removes the job instead). Force only a job that no process runs: the CLI and
the API can share one SQLite file, and a forced retry of a job another
process is still running starts a second run of it.

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
  (boot resilience — see PDR-003 in the audit pack). `domains_allow` /
  `domains_deny` match the URL's host at label boundaries: an allow entry
  admits the host or its subdomains, a deny entry blocks any host containing
  its labels in sequence (`x.com` doesn't block `fox.com`; `amazon.com` does
  block `amazon.com.au`). A request's `constraints.domains_allow` /
  `domains_deny` are matched the same way on top of the policy: a deny entry
  from either drops the URL, and a request allow list narrows the policy's
  (counted in `urls_dropped_policy`). Its `date_from` / `date_to` take an
  ISO 8601 date or datetime (no offset means UTC; anything else is a
  validation error) and apply to fresh and reused excerpts alike. The REST
  API and remote mode pass all of them through. `reputation` also takes `marketing_phrases`
  (whole-word; `[]` flags nothing), `primary_source_suffixes` (default
  `.gov`, `.edu`) and `penalize_conflict_of_interest` (default true) — see
  `policies/peer-reviewed.yaml`. `trusted_institutions` matches the host
  only, never the path, query or userinfo: a one-label entry such as
  `pubmed` matches any label of the host (`pubmed.ncbi.nlm.nih.gov`), so
  whoever owns a domain can earn it with a subdomain; prefer a full domain
  (`nih.gov`, `.gov`), which matches the host or its subdomains. The
  built-in peer-review tag is read from the host the same way.
- `taxonomies/` — taxonomy definitions for evidence classification. The API
  currently selects `taxonomies/wellbeing-8d.yaml` when present.
- `path_configs/` — output path definitions (tone, structure, depth per
  path). The API tries an operator-supplied file first (untracked), then
  falls back to the committed `path_configs/default.yaml`.

**Engine tuning** — describes *how the engine runs*:

- `config/` — the optional engine config YAML you pass with `--config`
  (e.g. `config/humanization_live.yaml`), plus two optional override files:
  `config/humanization_markers.yaml` (marker lists for the humanization
  scorer; replaces the lists packaged in
  `src/cce/config/humanization_markers.yaml` whole) and
  `config/model_pricing.yaml` (entries on top of the packaged price table).
  Neither has to exist: an installed wheel runs on the packaged copies.

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
| `CCE_LLM_TEMPERATURE` | `0.2` | Fallback sampling temperature; not sent to models that reject sampling params (Opus 4.7+, Sonnet 5, Fable 5) |
| `CCE_LLM_MAX_TOKENS` | unset | Per-call output token cap, thinking included. Unset = the model's maximum output, read from the Anthropic Models API once per process (128000 on the 4.6 and 5.x models, 64000 on the 4.5 ones), or 21000 if that lookup fails. Requests are streamed, so the SDK's ~21,333 non-streaming ceiling doesn't apply. A reply that hits the cap fails the job with an `IncompleteResponseError` naming the role and model; if thinking crowds out replies, lower the role's effort |
| `CCE_LLM_THINKING` | unset | `adaptive` or `disabled`, sent as `thinking: {type: ...}`. Unset = omit the param (model default: Sonnet 5 / Opus 5 think adaptively, the 4.6 models do not). Never sent to Opus 4.5, Haiku 4.5 or older. `adaptive` also drops `temperature` on the 4.6 models (the API rejects any value but 1 while thinking is on). Otherwise passed through as set, except `disabled` on Fable 5 / Opus 5.5, or on Opus 5 at effort `xhigh`/`max`, which the API rejects: that fails at start-up with a config error naming the model |
| `CCE_LLM_EFFORT` | unset | `low` / `medium` / `high` / `xhigh` / `max`, sent as `output_config.effort`. Unset = model default. Sent to Opus 4.5 and the 4.6 and later models; omitted on Haiku 4.5, Sonnet 4.5 and older. Opus 4.5 takes only `low`/`medium`/`high` (`xhigh`/`max` there fail at start-up with a config error); `xhigh` needs Opus 4.7+ / Sonnet 5 |

The writer's and verifier's replies are constrained to their JSON schemas
with structured outputs on every current model; there is no setting for it.
An unreadable reply is resent once, then fails the job.

### Verifier

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_VERIFIER_MODEL` | unset | Separate model for the verifier (YAML `verifier.model`), so writer and verifier blind spots aren't correlated. Credentials and the other `llm` settings are inherited, including `CCE_LLM_THINKING` / `CCE_LLM_EFFORT`, so those must also be valid for the verifier's model (e.g. effort `xhigh` fails on a 4.6 verifier); unset = the verifier uses `CCE_LLM_MODEL` |
| `CCE_VERIFIER_MAX_TOKENS` | unset | Per-call output cap for the verifier's claim-by-claim report (YAML `verifier.max_tokens`); unset = `CCE_LLM_MAX_TOKENS` |

### Per-role model settings

The writer, verifier and editor each take `model`, `max_tokens`, `thinking`
and `effort` (YAML `writer.*`, `verifier.*`, `humanization.editor.*`; env
`CCE_WRITER_*`, `CCE_VERIFIER_*`, `CCE_EDITOR_*` with the suffixes
`_MODEL`, `_MAX_TOKENS`, `_THINKING`, `_EFFORT`). Unset values inherit the
`CCE_LLM_*` ones, and credentials are always shared. A role with any setting
gets its own provider; the implied-claim checker uses the writer's, except
that its topic-extraction call is capped at 4096 output tokens (thinking
included) whatever `max_tokens` says, and a reply that hits that cap fails the
job. The values must suit that role's model (e.g. effort `xhigh` fails on a 4.6
model). With an injected `llm` (`ComponentOverrides`), setting any of these
raises `ValueError`: configure the injected provider instead.

A common tuning on Sonnet 5, where adaptive thinking can crowd out a long
rewrite: `CCE_EDITOR_EFFORT=medium`.

### Evidence store

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_EVIDENCE_BACKEND` | `sqlite` | Backend id (only `sqlite` implemented) |
| `CCE_EVIDENCE_SQLITE_PATH` | `evidence.db` | SQLite file (also jobs, packages with their full evidence, and API keys). Relative paths resolve against the working directory |

`CCE_EVIDENCE_SQLITE_PATH` is process-wide and overrides YAML, so it can't
keep tenants apart in one process: every engine or Pipeline that relies on it
shares one evidence pool and one job store. For per-tenant separation, give
each tenant its own stores — `CurationEngine.embedded(evidence_store=...,
job_store=...)` (injected stores are the caller's to connect and close), or
one `build_pipeline(config, registry, tenant_store, components)` per tenant.
Tenants may share one `ComponentSet` only while its crawl adapter is
tenant-neutral (a web adapter). `CrawlAdapter.search(query, limit)` takes no
tenant, so a document adapter in a shared set offers one tenant's documents
to every tenant's run: into its prompts, its package and its evidence store.
Give each tenant its own adapter instead:
`build_pipeline(config, registry, tenant_store, components, crawl_adapter=tenant_composite)`
replaces the set's adapter for that Pipeline only, and the LLM providers and
the rest of the set stay shared (with `CurationEngine.embedded()`, build one
engine per tenant with `overrides=ComponentOverrides(llm=shared_llm,
crawl_adapter=tenant_composite)`). Also run each such tenant under a source
policy whose `domains_allow` is non-empty and names that tenant's pseudo-host
(`acme` for `local://acme/...`), so another tenant's documents are refused even
if an adapter is shared by mistake: an empty `domains_allow` admits every
host. A non-empty list also limits web sources to the domains it names, so
list those too.

Schema v4 (B6) makes evidence unique on `(url, excerpt_hash)` rather than
`excerpt_hash` alone. An existing database is rebuilt losslessly the first
time the new code opens it (one transaction; back the file up first if you
want a copy of the old shape).

**URL reuse.** A URL that already has rows in the evidence store is not
crawled again: its stored rows join the run. They pass the current job's
recency, reputation and marketing filters like freshly crawled excerpts
(counted in the same `dropped_date` / `dropped_reputation` /
`dropped_marketing` metrics), with `max_age_days` measured from today rather
than from the day the page was crawled. Their source-quality flags
(peer-reviewed, primary source, marketing, reputation tier) are the ones
stored at crawl time, under the policy of the job that crawled the page;
they are not recomputed against the current policy's phrase and suffix
lists.

**Excerpt size.** A crawled page is cut into excerpts of at most 1,500
characters, at paragraph breaks, then line breaks, then (for a single line
longer than that, as in a transcript or a PDF text layer) the last
whitespace at or before the limit, or a hard cut where there is none. Every
excerpt stays a verbatim substring of the page. A stored row longer than the
limit (written before lines were bounded) is split the same way when its URL
is reused: the pieces keep the row's provenance and locator and take new
IDs, and the ones a job keeps are stored as new rows. The oversized row
stays in the store but no longer reaches a prompt.

### Crawl

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_CRAWL_ADAPTER` | `firecrawl` | Adapter id (only `firecrawl` implemented) |
| `CCE_CRAWL_API_KEY` | — | Overrides `FIRECRAWL_API_KEY` when set |
| `CCE_CRAWL_RATE_LIMIT` | `2.0` | Max concurrent crawl (scrape) requests per event loop for one Firecrawl API key: `int(value)`, minimum 1. A concurrency cap, not a per-second rate, despite the name; search requests are not counted. A host running several event loops at once (one per thread) gets the cap on each loop |
| `CCE_CRAWL_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `CCE_CRAWL_MAX_PER_SOURCE` | `5` | Max excerpts kept per source |
| `CCE_CRAWL_MAX_EVIDENCE` | `100` | Max evidence objects per request |

A failed search (bad key, no credits, rate limit, outage) is counted per
query in the DISCOVER record's `search_failed`, with `search_error` holding
the last failure's exception class name (never its message). A page that
could not be fetched, or answered HTTP 400 or above, counts in
`crawl_failed` and is never stored as evidence. A job that finds no
evidence after any search or crawl failed fails with error code
`crawl_unavailable` rather than `pipeline_error`.

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
| `CCE_API_HOST` | `0.0.0.0` | Bind address for `cce api start` (YAML `api.host`; the `--host` flag overrides both) |
| `CCE_API_PORT` | `8000` | Bind port (YAML `api.port`; the `--port` flag overrides both) |
| `CCE_API_REQUIRE_AUTH` | `true` | `false` disables bearer auth (dev only) |
| `CCE_API_CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `CCE_API_MAX_CONCURRENT_JOBS` | `2` | Parallel pipeline jobs |

### Job budget

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_MAX_TOKENS_PER_JOB` | unset | Token budget per job: input + output + cache-creation tokens of every LLM call, all paths and iterations (cache reads are not counted). Checked before each writer iteration, where a breach stops iterating and routes the job to `REVIEW_REQUIRED`, keeping partial drafts (ADR-003, audit-2026-06-09), and before each edit step, where a breach skips the edit so the verifier checks the writer's draft. Not a hard ceiling: past a passing check a job can still spend one writer call, or one edit step (one implied-claim call per contrastive frame that names a topic, plus the editor call), and then one verifier call. Unset = unlimited. |

### Cost estimate

Each job records an estimated LLM cost in USD: `cost_estimate_usd` on its
PUBLISH stage record, and `est_cost=$...` on the "Pipeline complete" log
line. It is priced per model from the token counts on the WRITE, VERIFY and
EDIT stage records (each names the model that answered), so a job whose
writer, verifier and editor run on different models is priced correctly.

Prices come from a table in USD per million tokens (input, output, cache
write, cache read): the packaged `src/cce/config/model_pricing.yaml`
(Anthropic first-party list prices), with the entries of
`config/model_pricing.yaml` in the working directory on top when that file
exists. Use the override when prices change, for Bedrock or Vertex AI rates,
or to price an injected provider's own model IDs. A model ID matches its own
entry, or the entry without a trailing `-YYYYMMDD` snapshot date; there is no
prefix matching, so a new model has no price until it is listed. The
estimate is `null` when any model the job used has no price (never a partial
sum), and when a provider reports no usage. It is an estimate at list
prices: batch or negotiated discounts and 1-hour cache writes are not
modelled.

### Publish policy

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_PUBLISH_POLICY` | `auto` | What a gate PASS on every path means (YAML `publish_policy`). `auto`: the job is `COMPLETED`. `human`: it is `READY_FOR_APPROVAL` — PASS is a quality signal and a person approves every output; `cce curate` exits 3, and `emit-mdx --job` needs `--force`. Process-wide, not per request |

### Humanization

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_HUMANIZATION_ENABLED` | `true` | Master switch for scorer/editor/checker (on by default since 2026-06-24; set `false` to skip all three) |
| `CCE_HUMANIZATION_MARKERS_PATH` | unset | Marker-lists file to use (must exist). Unset = `config/humanization_markers.yaml` in the working directory if present, else the lists packaged with cce |

Granular humanization thresholds are deliberately YAML-only (reviewable in
diffs); env vars exist only for the master switch and the marker path.

### Logging

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCE_LOG_FORMAT` | unset | `json` switches to structured JSON logs |

Pipeline log records carry `job_id`: for a job run by the engine or the API
it is the stored job's id, the one `cce status`, `cce jobs` and
`GET /v1/curate/jobs/{id}` show. A direct `Pipeline.run(request, policy,
job_id=...)` logs under the id it is given, or mints one.

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
