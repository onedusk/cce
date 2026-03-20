# Content Curation Engine -- Package Structure

Decided March 2026. Designed to support Phase 1 (core loop) without scaffolding, and extend naturally through Phase 4.

---

## Layout

Uses the `src` layout (`src/cce/`) so the package imports cleanly and doesn't conflict with tests.

```
cce/
├── pyproject.toml
├── .gitignore
├── README.md
├── docs/
│   └── internal/
├── src/
│   └── cce/
│       ├── __init__.py
│       ├── config/                  # Engine configuration (central entry point)
│       │   ├── __init__.py
│       │   ├── types.py             # Typed config objects (EngineConfig, LLMConfig, etc.)
│       │   └── loader.py            # Load from env vars / YAML, distribute to modules
│       │
│       ├── models/                  # Pydantic data contracts (shared)
│       │   ├── __init__.py
│       │   ├── request.py           # CurationRequest, constraints
│       │   ├── evidence.py          # Evidence, evidence metadata
│       │   ├── content.py           # ContentUnit, citations, evidence map
│       │   ├── job.py               # Job, JobStatus, JobStage
│       │   └── package.py           # PublishPackage, scores, lineage
│       │
│       ├── policy/                  # Source Policy (content rules, not engine config)
│       │   ├── __init__.py
│       │   ├── types.py             # SourcePolicy model, reputation/recency rules
│       │   └── loader.py            # Load policy from config (JSON/YAML)
│       │
│       ├── discovery/               # Discover + Extract + Normalize
│       │   ├── __init__.py
│       │   ├── discoverer.py        # Discovery logic, policy filters, extraction
│       │   └── adapters/
│       │       ├── __init__.py
│       │       ├── base.py          # CrawlAdapter protocol
│       │       └── firecrawl.py     # Firecrawl adapter (Phase 1 default)
│       │
│       ├── evidence/                # Evidence Store
│       │   ├── __init__.py
│       │   ├── store.py             # EvidenceStore protocol
│       │   └── sqlite.py            # SQLite implementation (Phase 1)
│       │
│       ├── tagging/                 # Structure & Tag (Phase 2)
│       │   ├── __init__.py
│       │   ├── plugins.py           # TaxonomyPlugin, PathPlugin protocols
│       │   └── tagger.py            # Applies plugins to evidence
│       │
│       ├── synthesis/               # Writer agent
│       │   ├── __init__.py
│       │   └── writer.py            # Evidence-constrained draft generation
│       │
│       ├── verification/            # Verifier agent + quality gate
│       │   ├── __init__.py
│       │   ├── verifier.py          # Claim-to-evidence checking, contradiction detection
│       │   └── gate.py              # Pass/fail/review routing, threshold config
│       │
│       ├── orchestrator/            # Pipeline execution
│       │   ├── __init__.py
│       │   └── pipeline.py          # Full pipeline, writer-verifier loop
│       │
│       ├── llm/                     # LLM provider adapters
│       │   ├── __init__.py
│       │   ├── base.py              # LLMProvider protocol
│       │   └── anthropic.py         # Anthropic adapter (Phase 1 default)
│       │
│       └── api/                     # REST API (Phase 3)
│           ├── __init__.py
│           ├── app.py               # FastAPI app factory
│           └── routes/
│               ├── __init__.py
│               ├── jobs.py          # /v1/curate/jobs endpoints
│               ├── evidence.py      # /v1/curate/evidence endpoints
│               └── health.py        # /v1/curate/health, /v1/curate/meta
│
└── tests/
    ├── __init__.py
    ├── test_discovery/
    ├── test_evidence/
    ├── test_synthesis/
    ├── test_verification/
    └── test_orchestrator/
```

---

## Module Responsibilities

| Module | Concern | Phase |
|---|---|---|
| `config/` | Engine configuration — env vars, YAML, typed config objects | 1 |
| `models/` | Pydantic data contracts shared by all modules | 1 |
| `policy/` | Source policy loading, types, per-topic overrides | 1 |
| `discovery/` | Source discovery, crawl planning, extraction, adapter interface | 1 |
| `evidence/` | Evidence persistence, dedup, retrieval | 1 |
| `synthesis/` | Evidence-constrained writer agent | 1 |
| `verification/` | Verifier agent + quality gate | 1 |
| `orchestrator/` | Pipeline execution, writer-verifier loop | 1 |
| `llm/` | LLM provider adapter interface | 1 |
| `tagging/` | TaxonomyPlugin + PathPlugin interfaces | 2 |
| `api/` | FastAPI REST endpoints + job queue | 3 |

---

## Architecture Block → Module Mapping

Every block from the architecture doc maps to exactly one module:

| Architecture Block | Module |
|---|---|
| CurationRequest (input contract) | `models/request.py` |
| Engine Configuration | `config/` |
| Source Policy | `policy/` |
| Discover Sources + Extract & Normalize | `discovery/` |
| Evidence Store | `evidence/` |
| Structure & Tag | `tagging/` |
| Synthesize Draft (writer) | `synthesis/` |
| Verify (critic) | `verification/verifier.py` |
| Quality Gate | `verification/gate.py` |
| Publish Package (output contract) | `models/package.py` |
| Workflow Orchestrator | `orchestrator/` |
| Crawl Adapters | `discovery/adapters/` |
| LLM Providers | `llm/` |
| REST API | `api/` |

---

## Design Decisions

### 1. `config/` is a centralized config entry point

Engine internals — LLM provider (API keys, model, temperature), evidence store (database path), crawl adapter (API keys, rate limits), quality gate thresholds — are all loaded from one place. `config/loader.py` reads from env vars or a YAML file and produces typed config objects that modules accept as constructor arguments. This is analogous to how `policy/` works for content rules, but for engine internals. Without this, config loading would scatter across `llm/anthropic.py`, `evidence/sqlite.py`, `discovery/adapters/firecrawl.py`, etc.

### 2. `models/` is separate from pipeline modules

Data contracts are shared across modules. Centralizing them avoids circular imports and makes contracts the single source of truth. All Pydantic models live here; pipeline modules import from `models/` but never define their own shared types.

### 3. Adapter protocols live next to their consumers

`discovery/adapters/base.py` defines the crawl adapter protocol. `evidence/store.py` defines the evidence store protocol. `llm/base.py` defines the LLM protocol. Implementations sit alongside the protocol in the same directory. This keeps the interface close to the code that uses it — you never have to look in a separate `interfaces/` package.

### 4. No `utils/` or `common/`

Shared data → `models/`. True cross-cutting concerns (if any emerge) go at the `cce/` package root. No junk-drawer module.

### 5. Extraction lives inside `discovery/`, not as a separate module

Extraction is tightly coupled to the crawl adapter's output format — what Firecrawl returns is structurally different from Crawl4AI, and normalization logic depends on that structure. In practice, extraction is either a method on the adapter or a step inside `discoverer.py`, not an independent pipeline stage. If a future adapter requires genuinely different extraction logic that doesn't fit the adapter pattern, it can be split out then — but starting with a separate `extraction/` module risks creating a thin passthrough that just reshapes adapter output into Evidence objects.

### 6. `verification/` contains both verifier and gate

The quality gate consumes the verifier's output directly. They're tightly coupled by design (the gate is the enforcement of the verifier's findings). Splitting them into separate top-level modules would add indirection without benefit.

### 7. `tagging/` is deferred to Phase 2

Phase 1 hardcodes any taxonomy/path logic needed for validation. The `tagging/` module gets built when we extract Thnk Labs specifics into the TaxonomyPlugin and PathPlugin interfaces.

### 8. `api/` is deferred to Phase 3

Phase 1 entry point is `orchestrator/pipeline.py` called directly. The FastAPI layer wraps the same orchestrator when we're ready for service deployment.

---

## Dependency Flow

```
config/  ← (no dependencies — reads env/YAML, produces typed objects)
models/  ← (no dependencies)
   ↑
policy/  ← models
   ↑
discovery/ ← models, policy, config, discovery/adapters
evidence/ ← models, config
   ↑
tagging/ ← models, evidence (Phase 2)
   ↑
synthesis/ ← models, evidence, llm, config
verification/ ← models, evidence, policy, llm, config
llm/ ← models, config
   ↑
orchestrator/ ← all pipeline modules, config
   ↑
api/ ← orchestrator, models, config (Phase 3)
```

No circular dependencies. `config/` and `models/` are the two roots — everything else depends on one or both of them, plus the modules above it in the pipeline.

---

## Phase 1 Entry Point

`orchestrator/pipeline.py` wires the core loop:

1. Load engine config → `config/loader.py`
2. Load source policy → `policy/loader.py`
3. Discover + extract sources → `discovery/discoverer.py` (via Firecrawl adapter)
4. Store evidence → `evidence/sqlite.py`
5. Write draft → `synthesis/writer.py` (via Anthropic LLM adapter)
6. Verify + quality gate → `verification/verifier.py` + `verification/gate.py`
7. Loop (verify → identify gaps → rewrite → verify) up to max iterations
8. Return `PublishPackage`

Single-threaded, no API, no plugins. Hardcode everything that Phase 2+ will make configurable.
