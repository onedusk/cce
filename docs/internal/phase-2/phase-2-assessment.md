# Phase 2 Assessment

> Living document. Captures current state, decisions made, and Phase 2 plan.
>
> Last updated: 2026-03-20

---

## 1. Phase 1 Status: Complete

**Core loop: proven across 3 wellness dimensions.** 7 live runs, last 4 all PASS on iteration 1.

| Metric | Range across runs 4-7 |
|--------|-----------------------|
| Confidence | 0.81 — 0.97 |
| Coverage | 0.90 — 0.97 |
| Diversity | 0.87 — 1.0 |
| Evidence leakage | 0% (all runs) |
| Pipeline time | 3.5 — 5 min |
| Evidence (capped) | 75 per run |

**Topics validated:** CBT-I (emotional/physical), financial literacy (financial), social connectedness (social). See `docs/internal/run-log.md` for full run history.

**Test suite:** 150 tests, all passing. Covers all pipeline modules.

**All P0-P4 bugs resolved:**
- P0 (JSON parsing): Fixed
- P1 (evidence volume): Fixed — simple cap shipped (746-1,440 raw → 75 capped)
- P2 (diversity formula): Fixed
- P3 (citation density): Manageable — all capped runs pass on iteration 1
- P4 (stage tracking): Fixed — per-iteration write/verify timing

**First-run-review docs** (`docs/internal/first-run-review/`) have been archived. They described bugs from runs 1-2, all resolved.

---

## 2. Decisions Made

### Evidence Capping Strategy: Two-Stage (decided 2026-03-20)

**Stage 1 (shipped):** Simple cap in `Discoverer._cap_evidence()` — per-source max (5 excerpts, longest preferred) + global cap (100 total). Config fields on `CrawlConfig`: `max_excerpts_per_source`, `max_evidence_total`.

**Stage 2 (Phase 2):** Embedding-based relevance ranking — score excerpts against topic query, keep top-K most relevant. Replaces length-based selection with semantic selection.

### Embedding Approach: Ollama + sqlite-vec (decided 2026-03-20)

**Embedding model:** `nomic-embed-text-v2-moe` (MoE architecture, 768 dims, already installed locally via Ollama)

**Embedding generation:**
- Ollama HTTP API (`POST http://localhost:11434/api/embed`) — no SDK needed, `httpx` already available
- Protocol-based `EmbeddingProvider` (like `LLMProvider`) for swappable backends

```
EmbeddingProvider (protocol)
  ├── OllamaEmbeddingProvider  (Phase 2 — local dev)
  └── APIEmbeddingProvider     (Phase 3 — deployment)
```

**Vector storage & search:** `sqlite-vec` (v0.1.7, 7.2k stars, MIT/Apache-2)
- SQLite extension that adds vector search to existing SQLite databases
- `pip install sqlite-vec` — one small dependency, loads via `sqlite_vec.load(db)`
- `vec0` virtual tables with built-in KNN via `WHERE embedding MATCH :query AND k = N`
- Built-in `vec_distance_cosine()` — no hand-written cosine similarity needed
- Metadata filtering during KNN queries (filter by URL, source quality, date)
- Works with `aiosqlite` — loads into the underlying `sqlite3.Connection`

**Why sqlite-vec over hand-written cosine:**
- Eliminates manual vector math — KNN search is a SQL query
- Metadata columns allow filtering during search (e.g., only peer-reviewed sources)
- Same DB for evidence + embeddings — no second system
- Deployment path stays SQLite: Turso and Cloudflare D1 both support sqlite-vec
- Avoids the eventual need for pgvector/Postgres entirely (unless scale demands it)

**Why not pgvector:**
- We're on SQLite everywhere (evidence store, local dev, planned deployment)
- Our vector scale is small (~75-1500 per run) — sqlite-vec handles this easily
- pgvector requires Postgres infrastructure — overkill for Phase 2-3
- sqlite-vec can be revisited if we outgrow SQLite's limits (unlikely for this use case)

**Dependencies:** `sqlite-vec` (PyPI package). Ollama is local infrastructure.

**Note:** Ollama must be running locally for embedding generation. Pipeline should fall back to length-based cap if embeddings are unavailable.

---

## 3. Phase 2 Scope

Three workstreams: plugin architecture, embedding ranking, and source policy refinement.

### 2.1: TaxonomyPlugin Interface

Formalize the 8 well-being dimensions as a plugin. Other products replace with their own taxonomy.

**Protocol exports:**
- Tag list (valid tags/categories)
- Classifier function (given evidence + content, assign tags)
- Optional hierarchy (parent-child relationships)

**Location:** `src/cce/tagging/`

### 2.2: PathPlugin Interface

Formalize Learn/Explore/Apply as a plugin. Each path is a different rendering strategy over the same evidence graph, not a separate pipeline.

**Protocol exports:**
- Path list (output paths)
- Rendering strategy per path
- Writer prompt overrides per path (tone, structure, depth)

### 2.3: Embedding-Based Evidence Ranking

Replace length-based cap with semantic relevance ranking using sqlite-vec.

**Components:**
- `EmbeddingProvider` protocol in `discovery/embeddings.py` + `OllamaEmbeddingProvider`
- `vec0` virtual table in `SQLiteEvidenceStore` for vector storage + KNN search
- Ranking integrated into `Discoverer.discover()` after extraction, before cap
- `sqlite-vec` extension loaded in `SQLiteEvidenceStore.connect()`

### 2.4: Source Policy Refinement

Policy loading is already config-driven. Remaining work:
- Add example policies for non-medical domains (financial, social, general)
- Document the policy authoring format

---

## 4. Phase 2 Architecture

### Where New Code Lives

```
src/cce/
├── discovery/
│   └── embeddings.py          ← NEW: EmbeddingProvider protocol + OllamaEmbeddingProvider
├── evidence/
│   └── sqlite.py              ← MODIFIED: load sqlite-vec, add vec0 table, embed + KNN search methods
├── tagging/                   ← NEW
│   ├── __init__.py
│   ├── taxonomy.py            ← TaxonomyPlugin protocol + registry
│   ├── paths.py               ← PathPlugin protocol + registry
│   └── plugins/               ← Reference implementations
│       ├── __init__.py
│       ├── wellbeing.py       ← Thnk Labs: 8 dimensions
│       └── learn_explore_apply.py  ← Thnk Labs: 3 paths
├── models/
│   ├── taxonomy.py            ← NEW: TaxonomyConfig, Dimension
│   └── path_config.py         ← NEW: PathConfig, OutputPath, RenderingStrategy
```

### Dependency Flow

```
config/ + models/ (roots, no deps)
  ↓
policy/ ← models
  ↓
discovery/ ← models, policy, config, adapters, embeddings
evidence/ ← models, config
  ↓
tagging/ ← models, evidence, llm, config
synthesis/ ← models, evidence, llm, config, tagging (receives path config)
verification/ ← models, evidence, policy, llm, config
  ↓
orchestrator/ ← all pipeline modules, config (wires tagging + ranking)
```

### Pipeline Flow

```
CurationRequest
  ↓
SourcePolicy → Discoverer → extract → simple cap
  ↓
EmbeddingProvider → embed excerpts → EvidenceStore (sqlite-vec vec0 table)
  ↓
KNN query (topic embedding MATCH, k=N) → relevance-ranked evidence
  ↓
TaxonomyPlugin.classify(evidence) → tagged evidence
  ↓
For each path in PathPlugin.paths:
  PathPlugin.get_writer_config(path) → writer overrides
  Writer(overrides) → Verifier → QualityGate
  ↓
PublishPackage (units tagged + path-specific)
```

---

## 5. Open Design Questions

### Where does tagging happen in the pipeline?

Before synthesis (classify evidence, writer uses tags for context) or after (classify finished content)? Affects how TaxonomyPlugin integrates with the orchestrator.

### Does the TaxonomyPlugin need an LLM call?

Rules-based classifier (keywords, domain patterns) is cheaper and deterministic. LLM classifier is more flexible but adds cost per run. Which for v1?

### How do PathPlugin writer overrides work?

Different system prompt per path? Different temperature/max length? Or a context string appended to the existing prompt?

### Should citation density threshold be lowered?

All capped runs pass on iteration 1, so this is low urgency. But 90% threshold could still cause unnecessary FAILs on edge cases. Consider lowering to 80%.

### Is humanization in or out of Phase 2?

Options: programmatic checks only (vocab, burstiness) in the quality gate, full LLM critic, or defer entirely to Phase 3.

---

## 6. Sequencing

### Phase 2 Implementation

| Order | Task | Effort | Dependency |
|-------|------|--------|------------|
| 2.1a | Define `TaxonomyConfig` + `Dimension` models | 1-2h | None |
| 2.1b | Define `TaxonomyPlugin` protocol | 1-2h | 2.1a |
| 2.1c | Implement well-being taxonomy plugin | 2-3h | 2.1b |
| 2.2a | Define `PathConfig` + `OutputPath` models | 1-2h | None |
| 2.2b | Define `PathPlugin` protocol | 1-2h | 2.2a |
| 2.2c | Implement Learn/Explore/Apply path plugin | 2-3h | 2.2b |
| 2.3a | Define `EmbeddingProvider` protocol + Ollama impl | 2-3h | None |
| 2.3b | Add sqlite-vec to evidence store (vec0 table, embed + KNN methods) | 2-3h | 2.3a |
| 2.3c | Integrate embedding ranking into discovery pipeline | 1-2h | 2.3b |
| 2.4 | Wire tagging + ranking into orchestrator | 2-4h | 2.1c, 2.2c, 2.3c |
| 2.5 | Update writer to accept path-specific config | 1-2h | 2.2c |
| 2.6 | Tests for all new code | 4-6h | All above |

**Parallelizable:** 2.1, 2.2, and 2.3 tracks are independent. 2.4 is the convergence point.

---

## 7. Explicitly Deferred (Phase 3+)

- REST API layer
- TypeScript SDK
- Webhook system
- Embedded SDK mode
- Plugin CRUD via API
- Multi-tenant isolation
- A2A support
- LLM-based humanization critic
- Deployment infrastructure (hosting, API-based embeddings)
- pgvector / Postgres — only if sqlite-vec + Turso/D1 can't handle scale

---

## Source Documents

| Document | Key Contribution |
|----------|-----------------|
| `content-curation-engine.md` | Thnk Labs architecture, Learn/Explore/Apply paths |
| `content-curation-engine-generic.md` | Plugin interfaces, configuration-driven design |
| `content-curation-engine-next-steps.md` | Phase roadmap, sequencing, deferral decisions |
| `content-curation-engine-landscape.md` | Market validation, gap analysis |
| `package-structure.md` | Directory layout, dependency flow |
| `run-log.md` | All live run metrics and observations |
| `research/ai_writing_vs_human_writing.md` | AI fingerprint science |
| `research/mitigations.md` | Humanization strategies |
| `tests/test-plan.md` | Test coverage blueprint |
