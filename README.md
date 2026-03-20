# Content Curation Engine - CCE

An evidence-first content curation engine with enforced citations. Scaling content production without scaling misinformation.

The engine discovers sources, extracts verbatim evidence with provenance, synthesizes citation-backed drafts, and verifies every claim before publishing. If a claim can't be traced to stored evidence, it doesn't ship.

## Architecture

```
CurationRequest
  → Source Policy (filter bad inputs early)
  → Discover + Extract (crawl, normalize, store evidence)
  → Evidence Store (verbatim excerpts + provenance)
  → Structure & Tag (pluggable taxonomy)
  → Writer (draft from evidence only)
  → Verifier (check every claim against evidence)
  → Quality Gate (pass / fix gaps / human review)
  → Publish Package (content + evidence map + scores + lineage)
```

**Core invariant:** the writer produces drafts _only_ from stored evidence objects. The verifier is a separate role that checks every claim. The quality gate enforces "no citation, no ship."

## Package Structure

```
src/cce/
├── config/         # Engine configuration (env vars, YAML → typed objects)
├── models/         # Pydantic data contracts (shared across all modules)
├── policy/         # Source policy (domain rules, reputation, recency)
├── discovery/      # Source discovery + extraction + crawl adapters
├── evidence/       # Evidence store (persistence, dedup, retrieval)
├── synthesis/      # Writer agent (evidence-constrained drafts)
├── verification/   # Verifier agent + quality gate
├── orchestrator/   # Pipeline execution, writer-verifier loop
├── llm/            # LLM provider adapters
├── tagging/        # Taxonomy + path plugins (Phase 2)
└── api/            # REST API via FastAPI (Phase 3)
```

See [`docs/internal/package-structure.md`](docs/internal/package-structure.md) for module responsibilities, dependency flow, and design decisions.

## Key Design Points

- **Evidence-first** -- everything is an evidence object before it becomes content
- **Policy-driven intake** -- quality is enforced at discovery, not patched after
- **Writer/critic separation** -- synthesis and verification are distinct roles
- **No citation, no ship** -- the quality gate that prevents misinformation at scale
- **Plugin boundaries** -- taxonomy, output paths, and platform integration are extension points
- **Adapters, not abstractions** -- external deps (crawlers, LLMs, MCP) are behind adapter interfaces that live next to their consumers

## Implementation Phases

| Phase | Focus                                                             | Status      |
|-------|-------------------------------------------------------------------|-------------|
| 1     | Core loop -- discover, extract, store, write, verify, gate        | Not started |
| 2     | Plugin extraction -- TaxonomyPlugin, PathPlugin, policy config    | --          |
| 3     | API layer -- REST endpoints, job orchestration, SDK               | --          |
| 4     | Platform integration -- storage adapter, feedback loop, rendering | --          |

## Tech Stack

- **Python** -- core engine and pipeline
- **FastAPI** -- REST API (Phase 3)
- **Pydantic** -- data contracts and configuration
- **SQLite** -- evidence store (Phase 1 local dev)
- **Firecrawl** -- crawl adapter (Phase 1 default)

## Internal Docs

- [`content-curation-engine.md`](docs/internal/content-curation-engine.md) -- original architecture (Thnk Labs-specific)
- [`content-curation-engine-generic.md`](docs/internal/content-curation-engine-generic.md) -- reusable framework spec, API design, data contracts
- [`content-curation-engine-landscape.md`](docs/internal/content-curation-engine-landscape.md) -- landscape research (STORM, Perplexity, Elicit, Loki, etc.)
- [`content-curation-engine-next-steps.md`](docs/internal/content-curation-engine-next-steps.md) -- phased implementation plan
- [`package-structure.md`](docs/internal/package-structure.md) -- module layout, dependency flow, design decisions
