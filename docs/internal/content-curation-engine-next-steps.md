# Content Curation Engine -- Next Steps

Based on the architecture spec and landscape research. Sequenced to prove the riskiest assumptions first and defer abstraction until the core loop works.

---

## Phase 1: Prove the Core Loop

The goal is a single-threaded script that takes a topic, discovers sources, stores evidence, writes a draft from that evidence, verifies citations, and passes or fails a quality gate. No API, no plugins, no taxonomy system. Hardcode everything.

### 1.1 Evidence Store schema + Discover -> Extract -> Store loop

Build the foundation that no existing tool provides. Pick a topic from Thnk Labs content needs and run it end-to-end through discovery and extraction.

- Define the Evidence table schema (id, url, title, author, publishedAt, retrievedAt, excerpt, locator, contentHash for dedup).
- Wire up Crawl4AI or Firecrawl behind a thin adapter function. Start with one; the adapter interface means swapping later is cheap.
- Write a Source Policy as a plain config object (allowed domains, blocked domains, recency cutoff). Apply it as a filter before extraction, not after.
- Store extracted evidence in SQLite or Cloudflare D1 (depending on where the rest of the stack lives).
- Validate by inspecting the store: are the excerpts verbatim? Is provenance complete? Is dedup working?

Reference: Crawl4AI (https://github.com/unclecode/crawl4ai), Firecrawl (https://github.com/mendableai/firecrawl)

### 1.2 Evidence-constrained writer agent

This is the hardest unsolved problem in the landscape. No existing system reliably enforces that the writer uses only stored evidence and doesn't fill gaps from training data.

- Build the writer as a function that receives a list of Evidence objects and a topic, and outputs a draft with inline citations keyed to evidence IDs.
- Experiment with prompt structures: providing evidence as numbered blocks, instructing the model to cite by number, explicitly forbidding claims not backed by a provided excerpt.
- Test for "evidence leakage" -- run the writer on a topic where the evidence store is intentionally incomplete and check whether it fills gaps from general knowledge.
- Iterate on the prompt and output format until leakage is below an acceptable threshold.

Reference: STORM's outline-first approach (https://github.com/stanford-oval/storm) for how they structure the evidence-to-draft step.

### 1.3 Verifier agent + quality gate

Build the critic as a separate function that receives the draft and the evidence store, and produces a verification report.

- For each claim in the draft, check that a matching citation exists and that the citation resolves to a stored evidence excerpt.
- Flag contradictions between cited sources.
- Assign a confidence score per claim and aggregate to a document-level score.
- Implement the quality gate: pass (autopublish), fail (return to writer with specific gaps), or review (below threshold, needs human eyes).
- Wire the writer-verifier feedback loop: verify -> identify gaps -> rewrite -> verify again, with a max iteration count.

Reference: Loki's 5-step verification pipeline (https://github.com/Libr-AI/OpenFactVerification), Novel-OS's agent separation pattern (https://github.com/mrigankad/Novel-OS)

### 1.4 End-to-end validation

Run the full pipeline on 3-5 real topics from Thnk Labs content. For each run, manually audit:

- Are all citations real and accurate?
- Did the writer stay within the evidence boundary?
- Did the verifier catch the gaps you know exist?
- Does the quality gate route correctly (pass/fail/review)?
- Is the evidence map (claim -> source) actually useful for auditing?

This is the gate before investing in anything else. If the core loop doesn't produce trustworthy, citation-backed content, the rest of the architecture doesn't matter.

---

## Phase 2: Extract the Thnk Labs Specifics into Config

Once the core loop works with hardcoded Thnk Labs settings, pull those settings out into the plugin interfaces defined in the architecture doc.

### 2.1 TaxonomyPlugin interface

- Define the interface: tag list, classifier function, optional hierarchy.
- Move the 8 well-being dimensions into a TaxonomyConfig that implements the interface.
- Test with a second taxonomy (even a simple one) to prove the interface works generically.

### 2.2 PathPlugin interface

- Define the interface: path list, rendering strategy, optional writer prompt overrides.
- Move Learn / Explore / Apply into a PathConfig.
- Test with a second path set.

### 2.3 Source Policy as config file

- Move the hardcoded domain lists, reputation rules, and recency rules into a JSON/YAML config file that the engine loads at startup.
- Add per-topic overrides.

---

## Phase 3: API Layer

Wrap the pipeline in the REST API and SDK defined in the architecture doc.

### 3.1 REST API (curation workflow only)

- POST /v1/curate/jobs -- submit a CurationRequest, get back a job ID
- GET /v1/curate/jobs/:jobId -- poll status
- GET /v1/curate/jobs/:jobId/package -- retrieve the publish package
- POST /v1/curate/jobs/:jobId/retry -- retry from last checkpoint
- GET /v1/curate/health and /v1/curate/meta

Skip webhooks, evidence search, and job listing for now. Add them when a consumer needs them.

### 3.2 Job orchestration

- Add stage checkpointing so retries don't restart from scratch.
- Add basic job queue (even an in-memory queue to start; swap to a durable queue later).
- Add lineage tracking (policy version, engine version, run ID, timestamps per stage).

### 3.3 SDK (remote mode only)

- Typed TypeScript client that wraps the REST API.
- Skip embedded mode until there's a real use case for it.

---

## Phase 4: Platform Integration (Thnk Labs)

Connect the engine to the Thnk Labs platform as the first consumer.

### 4.1 Content Store adapter

- Write the adapter that takes a PublishPackage and persists it in whatever Thnk Labs uses for content storage.

### 4.2 Feedback loop

- Wire usage signals (flags, views, ratings) from the platform back to the engine to trigger re-curation.

### 4.3 Content assembly

- Build the rendering layer that takes a PublishPackage and displays it in the Thnk Labs UI, with citations and evidence map accessible to users.

---

## Explicitly Deferred

These are real features but not worth building until the core is proven and a consumer is live.

- **A2A support** -- adds complexity before there's a second agent to talk to.
- **Webhook system** -- polling is fine for early consumers.
- **Embedded SDK mode** -- service deployment first; add embedded when someone needs it.
- **Plugin CRUD via API** -- config files are fine until multiple teams are registering taxonomies dynamically.
- **Multi-tenant isolation** -- build for single-tenant first; add tenant boundaries when a second product embeds the engine.

---

## Decisions Made

- **Language/runtime**: Python for Phase 1 through Phase 3. The reference implementations we'll be studying and borrowing from (Crawl4AI, Loki, STORM, LlamaIndex) are all Python, and being in the same language means lifting code directly instead of translating. FastAPI for the REST API in Phase 3. The TS data contracts in the architecture doc translate 1:1 to Pydantic models. A TS SDK client can be added in Phase 3 if needed -- it's a thin wrapper over HTTP.

## Decision Points (need answers before or during Phase 1)

- **Evidence store**: SQLite for local dev, Cloudflare D1 for deployment? Or something else entirely?
- **LLM provider**: Anthropic, OpenAI, or both behind an adapter from the start?
- **Hosting**: Cloudflare Workers (you have the MCP connected), Fly.io (also connected), or something else? Note: Python rules out Cloudflare Workers (JS/TS only), so Fly.io or a traditional VPS may be the natural fit.
