# Phase 2 Assessment

> Synthesized from all internal docs. Captures where we are, what must happen before Phase 2, and how Phase 2 should be structured.

---

## 1. Phase 1 Status

**Core loop: proven.** The pipeline runs end-to-end: Discover → Extract → Store → Write → Verify → Gate → Publish.

**Most recent run** (March 19, 2026 — 3rd run, CBT-I topic, `output/run_2bbfadf44ed4/`):
- Status: `completed` (PASS on iteration 2)
- 785 evidence excerpts from 15 trusted sources in 2.7s discovery
- ~4,000-word article with dense inline citations
- Confidence: 0.986, Coverage: 0.986, Diversity: 1.0
- Gate 1: FAIL (0.971 confidence, citation density 86% < 90% threshold)
- Gate 2: PASS (0.986 confidence, density met)
- Total pipeline time: ~9.5 minutes (write-verify loop dominates)

**Test suite: complete.** 147 tests across 11 files, all passing in 0.15s. Covers parsing, models, discovery (unit + integration), quality gate, verifier, writer, SQLite storage, config/policy loading, pipeline orchestration, and output serialization.

**What's NOT done from Phase 1:**
- End-to-end validation on 3-5 real topics (3 runs completed on CBT-I, need additional topics)
- Two outstanding issues from first-run review (see Section 2)

**Bug fix status** (verified against 3rd run — `output/run_2bbfadf44ed4/`):

| Bug | Status | Evidence from 3rd run |
|-----|--------|----------------------|
| P0: JSON parsing failure | **FIXED** | Both iterations parsed. Gate 1: 0.971, Gate 2: 0.986. Status: `completed` |
| P1: Evidence volume | **OPEN** | 785 excerpts (was 794). No capping implemented |
| P2: Diversity formula | **FIXED** | Diversity = 1.0 (was 0.019) |
| P3: Citation density | **Manageable** | Still triggers FAIL on iter 1 (86% density < 90% threshold), but iter 2 passes. Loop recovers |
| P4: Stage tracking | **OPEN** | Stages: discover, write, publish. No per-iteration tracking |

---

## 2. Remaining Work Before Phase 2

Two issues worth addressing before adding Phase 2 abstractions:

### P1: Evidence Volume Too High

**Problem:** 785 excerpts from paragraph-level chunking. No relevance filtering between discovery and synthesis. Writer and verifier receive all 785 in prompts.

**Impact:** Higher cost, slower pipeline (~10 min write-verify), scalability risk as topics grow.

**Fix options:**
- **Short-term:** Simple cap — dedup by content hash + per-source max (e.g., top 5 excerpts per URL) → ~75 excerpts
- **Long-term:** Embedding-based relevance ranking — score excerpts against topic, keep top-K

**Decision needed:** Which approach first? Simple cap is faster but less precise. Embedding approach is more robust but adds a dependency.

**Ref:** project memory `project_evidence_capping.md`

### P4: Write-Verify Loop Not Tracked in Stage Records

**Problem:** Stages show only `discover` (2.7s), `write` (9m 36s), `publish` (<1s). No per-iteration breakdown within the write stage.

**Impact:** No visibility into per-iteration timing. Hard to diagnose performance.

**Fix:** Record `StageRecord` per write-verify iteration in `pipeline.py`.

### Resolved / Manageable

- **P0 (JSON parsing):** Fixed. Verifier JSON parsed correctly in all 3 runs since fix.
- **P2 (diversity formula):** Fixed. Confirmed `1.0` in 3rd run.
- **P3 (citation density):** Still causes FAIL on iteration 1 when density is 86% (threshold 90%), but the write-verify loop handles it — iteration 2 passes. Could lower `min_citation_density_ratio` from 0.9 to 0.8 if the extra iteration is costly, but the loop is working as designed.

### Note on first-run-review docs

The documents in `docs/internal/first-run-review/` describe bugs from the 1st run (March 17). Most were fixed by the 3rd run (March 19, `output/run_2bbfadf44ed4/`). These docs should be archived or annotated as historical — they no longer reflect current state.

---

## 3. Phase 2 Scope

Phase 2 extracts Thnk Labs-specific logic into pluggable configuration. Three workstreams from `content-curation-engine-next-steps.md`:

### 2.1: TaxonomyPlugin Interface

**What:** Formalize the 8 well-being dimensions as a plugin that other products can replace with their own taxonomy (practice areas for legal, product categories for e-commerce, etc.).

**Protocol exports:**
- Tag list (the set of valid tags/categories)
- Classifier function (given evidence + content, assign tags)
- Optional hierarchy (parent-child relationships between tags)

**Location:** `src/cce/tagging/` (directory already planned in `package-structure.md`)

**Design from `content-curation-engine-generic.md`:**
```
TaxonomyConfig:
  id: str
  dimensions: list[Dimension]  # tag definitions
  classifier: ClassifierConfig  # LLM prompt or rules-based
```

### 2.2: PathPlugin Interface

**What:** Formalize Learn/Explore/Apply as a plugin. Each path is a different rendering strategy over the same evidence graph, not a separate pipeline.

**Protocol exports:**
- Path list (the set of output paths)
- Rendering strategy per path
- Writer prompt overrides per path (tone, structure, depth)

**Key principle:** "Treat your output paths as different renderers over the same evidence graph, not separate research pipelines." (from `content-curation-engine.md`)

**Design from `content-curation-engine-generic.md`:**
```
PathConfig:
  id: str
  paths: list[OutputPath]  # path definitions
  rendering: dict[str, RenderingStrategy]  # per-path writer config
```

### 2.3: Source Policy as Config

**What:** Move any remaining hardcoded source rules to YAML configuration with per-topic overrides.

**Status:** Largely done in Phase 1. `policy/loader.py` already loads from YAML. `policies/peer-reviewed.yaml` exists as reference. `TopicOverride` supports per-topic domain/reputation/recency adjustments. The `Discoverer._resolve_overrides()` method applies them.

**Remaining work:** Verify that all policy logic is config-driven, add additional example policies, document the policy authoring format.

---

## 4. Phase 2 Architecture

### Where New Code Lives

```
src/cce/
├── tagging/                    ← NEW (Phase 2)
│   ├── __init__.py
│   ├── taxonomy.py             ← TaxonomyPlugin protocol + registry
│   ├── paths.py                ← PathPlugin protocol + registry
│   └── plugins/                ← Reference implementations
│       ├── __init__.py
│       ├── wellbeing.py        ← Thnk Labs: 8 dimensions
│       └── learn_explore_apply.py  ← Thnk Labs: 3 paths
├── models/
│   ├── taxonomy.py             ← NEW: TaxonomyConfig, Dimension, ClassifierConfig
│   └── path_config.py          ← NEW: PathConfig, OutputPath, RenderingStrategy
```

### Dependency Flow (Extended)

```
config/ + models/ (roots, no deps)
  ↓
policy/ ← models
  ↓
discovery/ ← models, policy, config, adapters
evidence/ ← models, config
  ↓
tagging/ ← models, evidence, llm, config       ← NEW
synthesis/ ← models, evidence, llm, config, tagging  ← MODIFIED (receives path config)
verification/ ← models, evidence, policy, llm, config
  ↓
orchestrator/ ← all pipeline modules, config    ← MODIFIED (wires tagging into pipeline)
```

### Pipeline Flow (Extended)

```
CurationRequest
  ↓
SourcePolicy → Discoverer → EvidenceStore
  ↓
TaxonomyPlugin.classify(evidence) → tagged evidence    ← NEW
  ↓
For each path in PathPlugin.paths:
  PathPlugin.get_writer_config(path) → writer overrides  ← NEW
  Writer(overrides) → Verifier → QualityGate
  ↓
PublishPackage (units tagged + path-specific)
```

---

## 5. Open Design Questions

### Evidence Capping (P1)

| Approach | Pros | Cons |
|----------|------|------|
| Simple cap (dedup + per-source max) | Fast to implement, no new deps | Misses relevance — top 5 per source may not be the best 5 |
| Embedding-based ranking | Precise, topic-aware | Adds embedding model dependency, latency, cost |
| Two-stage (simple cap first, embeddings later) | Incremental, unblocks Phase 2 | Two implementations to maintain |

**Recommendation:** Simple cap first (P1 fix), embedding-based as Phase 2 enhancement when TaxonomyPlugin needs relevance scoring anyway.

### Humanization Verifier

From `docs/internal/research/mitigations.md`: a separate LLM critic that checks for AI fingerprints (vocabulary, burstiness, structural rigidity) and sends flagged sections back to the writer.

| Integration point | Pros | Cons |
|-------------------|------|------|
| Parallel with citation verifier | Doesn't add iterations | Two LLM calls per verify step |
| Sequential after citation verifier | Simpler flow | Adds iteration if humanization fails |
| Quality gate expansion (programmatic only) | No extra LLM cost | Limited to measurable signals (vocab, burstiness) |

**Recommendation:** Start with programmatic checks in the quality gate (vocab frequency, sentence length variance). Defer LLM-based humanization critic to Phase 2.5 or Phase 3.

### Deployment Infrastructure

From `content-curation-engine-next-steps.md`:
- Evidence store: SQLite local → Cloudflare D1 for deployment?
- LLM provider: Anthropic only → OpenAI adapter?
- Hosting: Fly.io vs. traditional VPS?

**Recommendation:** Defer deployment decisions to Phase 3 (API layer). Phase 2 is about abstractions, not infrastructure. Keep SQLite + Anthropic for now.

---

## 6. Sequencing Recommendation

### Before Phase 2 (Bug Fixes)

| Priority | Task | Effort | Dependency |
|----------|------|--------|------------|
| P0 | Fix verifier JSON parsing | 1-2 hours | None |
| P1 | Simple evidence cap (dedup + per-source max) | 2-4 hours | None |
| P3 | Soften citation density (threshold adjustment) | 30 min | None |
| P4 | Add write-verify stage tracking | 1-2 hours | None |
| — | Run end-to-end on 2-4 more topics | 2-3 hours | P0 fix |

P2 (diversity formula) is already fixed. These can be parallelized — P0, P1, P3, P4 are independent.

### Phase 2 (Plugin Architecture)

| Order | Task | Effort | Dependency |
|-------|------|--------|------------|
| 2.1a | Define `TaxonomyConfig` + `Dimension` models in `models/` | 1-2 hours | None |
| 2.1b | Define `TaxonomyPlugin` protocol in `tagging/taxonomy.py` | 1-2 hours | 2.1a |
| 2.1c | Implement Thnk Labs well-being taxonomy plugin | 2-3 hours | 2.1b |
| 2.2a | Define `PathConfig` + `OutputPath` models in `models/` | 1-2 hours | None |
| 2.2b | Define `PathPlugin` protocol in `tagging/paths.py` | 1-2 hours | 2.2a |
| 2.2c | Implement Learn/Explore/Apply path plugin | 2-3 hours | 2.2b |
| 2.3 | Wire tagging into pipeline orchestrator | 2-4 hours | 2.1c, 2.2c |
| 2.4 | Update writer to accept path-specific config | 1-2 hours | 2.2c |
| 2.5 | Add programmatic humanization checks to gate | 2-3 hours | Independent |
| 2.6 | Tests for all new code | 4-6 hours | All above |

**Parallelizable:** 2.1 and 2.2 tracks can run in parallel. 2.5 is independent.

**Total estimated effort:** ~20-30 hours for full Phase 2.

---

## 7. Explicitly Deferred

These are Phase 3+ concerns. Do not scope into Phase 2.

- **REST API layer** — endpoints, job orchestration, stage checkpointing
- **TypeScript SDK** — remote mode client
- **Webhook system** — polling is fine for early consumers
- **Embedded SDK mode** — service deployment first
- **Plugin CRUD via API** — config files are adequate
- **Multi-tenant isolation** — single-tenant first
- **A2A support** — add when second agent needed
- **LLM-based humanization critic** — start with programmatic checks

---

## Source Documents

| Document | Key Contribution |
|----------|-----------------|
| `content-curation-engine.md` | Thnk Labs-specific architecture, Learn/Explore/Apply paths |
| `content-curation-engine-generic.md` | Plugin interfaces (TaxonomyPlugin, PathPlugin), configuration-driven design |
| `content-curation-engine-next-steps.md` | Phase roadmap, sequencing, deferral decisions |
| `content-curation-engine-landscape.md` | Market validation, gap analysis, competitive positioning |
| `package-structure.md` | Directory layout, dependency flow, `tagging/` placement |
| `first-run-review/run-summary.md` | Live execution data (794 excerpts, 64 citations, 0% leakage) |
| `first-run-review/findings.md` | P0-P5 issues with root causes |
| `first-run-review/recommendations.md` | Fix prioritization and approaches |
| `research/ai_writing_vs_human_writing.md` | AI fingerprint science, detection methods |
| `research/mitigations.md` | Vocabulary suppression, burstiness, humanization verifier design |
| `tests/test-plan.md` | Test coverage blueprint (147 tests, all implemented) |
