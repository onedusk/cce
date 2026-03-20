# Content Curation Engine -- Landscape Research

Research conducted March 2026. The question: does an integrated, embeddable, evidence-first content curation engine already exist?

Short answer: no, none that we have found. The full pipeline (discover -> extract -> evidence store -> tag -> write -> verify -> publish) does not exist as a single reusable system. What exists are strong components that cover 1-2 stages each, plus a few closed products that come close but aren't embeddable.

---

## Closest Existing Products (Closed)

### STORM (Stanford) -- the nearest open match

- https://github.com/stanford-oval/storm

Stanford's STORM project is the closest open-source analog. It generates full-length articles with citations by first researching a topic on the internet, collecting references, generating a hierarchical outline, then synthesizing a full article with inline citations. It has four customizable modules (Knowledge Curation, Outline Generation, Article Generation, Article Polishing), and each module interface can be swapped.

Where it falls short: no built-in writer/verifier separation, no evidence storage layer with provenance tracking, no quality gate enforcement, and no configurable source policies. It generates citations but doesn't verify them against stored evidence. It's closer to "good RAG article writer" than "auditable curation engine."

### Perplexity AI (ProSearch)

- https://www.perplexity.ai/

Architecturally the closest product to what we're designing. Citations are embedded structurally during generation (not retrofitted), it uses a hybrid retrieval pipeline (lexical + semantic + vector), and the multi-document research mode produces dense claim-to-source mapping. However, it's a closed SaaS product with no embeddable component, no public evidence store, and no explicit writer/verifier role separation.

### Elicit (by Ought)

- https://elicit.com/

Research assistant purpose-built for evidence synthesis on academic literature. Searches 138M papers, extracts data from full-text with sentence-level citations, and uses process-based evaluation (emphasizing truth-seeking over outcome). Philosophical alignment with our engine is strong. Practical limitations: scoped to academic literature only, not general web; tabular output rather than evidence graph; not embeddable.

### QIAGEN Digital Insights

- https://digitalinsights.qiagen.com/

Hybrid AI + expert curation for genomics knowledge. Stores facts as structured triples with provenance. Graph ML detects contradictions and flags for human review. Expert biocurators validate; adjudications feed back to training. This is the only production system found that actually implements writer/verifier separation and claim-to-source auditing. But it's proprietary, domain-locked to genomics, and enterprise-only.

### Consensus AI

- https://consensus.app/

Searches 220M peer-reviewed papers with auto-citations. Their Scholar Agent uses a multi-agent architecture. Limited to peer-reviewed sources, no claim-to-source map output, and not embeddable.

---

## Open-Source Components (by pipeline stage)

### Discovery & Crawling

**Crawl4AI** -- Open-source, runs locally, outputs LLM-ready Markdown. JavaScript rendering via Playwright, multiple extraction strategies (CSS, XPath, LLM-based), topic-based chunking. Solid and battle-tested.
- https://github.com/unclecode/crawl4ai

**Firecrawl** -- API-first service with an open-source option. Five endpoints (scrape, crawl, search, map, agent). Designed for token-efficient LLM inputs. Good quality, but API-dependent.
- https://github.com/mendableai/firecrawl

Both handle discovery/extraction well. Neither stores evidence with provenance or feeds into a synthesis pipeline.

### Fact-Checking & Verification

**Loki / OpenFactVerification** (MIT license) -- 5-step pipeline: claim decomposition, check-worthiness assessment, query generation, evidence retrieval (real-time web search), claim verification. The closest open tool to our Verifier block. Published at COLING 2025. Limitation: verification only, no content synthesis.
- https://github.com/Libr-AI/OpenFactVerification

**FactAgent** -- 4-agent fact-checking system. Input Ingestion (claim decomposition), Query Generation, Evidence Retrieval, Verdict Prediction. Tested on FEVEROUS, HOVER, SciFact benchmarks with 12.3% F1 improvement. Good retrieval, no synthesis.
- https://github.com/HySonLab/FactAgent

**ClaimeAI** (built on LangGraph) -- Parses text, extracts claims, searches web for evidence, generates accuracy reports. Post-generation verification tool.
- https://github.com/BharathxD/ClaimeAI

### Multi-Agent Writing Architectures

**Novel-OS** (MIT license) -- 5-agent editorial pipeline: Architect (planner), Scribe (writer), Editor, Continuity Guardian (fact-checker), Style Curator. Persistent state management, human-in-the-loop approval gates. Strongest existing example of writer/verifier separation in open source. But designed for creative fiction, not evidence-based content curation. No web crawling, no evidence storage.
- https://github.com/mrigankad/Novel-OS

### RAG Frameworks

**LlamaIndex** -- Has a CitationQueryEngine that tracks source nodes and produces numbered citations. Also has Workflows 1.0 (event-driven, async-first) and a multi-agent example of Researcher + Writer. Good building blocks, but assembling them into a full curation pipeline is a significant custom effort.
- https://github.com/run-llama/llama_index

**LangChain / LangGraph** -- Self-RAG pattern where LLMs generate self-reflection tokens confirming statement support. Agentic RAG with multiple tool integration. Framework, not a solution; requires heavy customization.
- https://github.com/langchain-ai/langchain
- https://github.com/langchain-ai/langgraph

**Self-RAG** -- Reference implementation for retrieval-augmented generation with self-reflection. The model decides when to retrieve, then grades its own retrieved passages and generations for support and relevance.
- https://github.com/AkariAsai/self-rag

**RAGFlow** -- Full RAG pipeline from ingestion to QA. Good document processing, but no publishing workflow, no multi-agent architecture, no citation enforcement.
- https://github.com/infiniflow/ragflow

**Pathway** -- Real-time indexing with change tracking. Detects document changes, re-parses, updates embeddings automatically. Interesting for the re-curation trigger concept, but indexing/retrieval only.
- https://github.com/pathwaycom/pathway

### Citation-Focused Research Projects

**ALCE** (Princeton NLP, EMNLP 2023) -- First benchmark for evaluating LLM citation quality. Research project, not a usable system.
- https://github.com/princeton-nlp/ALCE

**LongCite** (THUDM) -- Fine-grained sentence-level citations in long-context QA. Provides fine-tuned models (glm4-9b, llama3.1-8b). QA-focused, not publishing.
- https://github.com/THUDM/LongCite

**FEVER** (academic benchmark) -- 185K claims with Supported/Refuted/NotEnoughInfo labels, sentence-level evidence annotation. Reference architecture for claim verification, but a dataset/benchmark, not a system.
- https://github.com/awslabs/fever

---

## Gap Analysis

| Pipeline Stage | Best Open Option | What's Missing |
|---|---|---|
| Configurable source discovery | Crawl4AI, Firecrawl | No policy layer (allow/deny, reputation, recency rules) |
| Extract & normalize | Crawl4AI, LlamaIndex loaders | No standard provenance schema |
| Evidence store with excerpts | None | No open system stores verbatim excerpts with full provenance |
| Taxonomy tagging | LlamaIndex (custom) | No pluggable taxonomy interface |
| Evidence-grounded synthesis | STORM | No enforcement that writer uses only stored evidence |
| Writer/verifier separation | Novel-OS (pattern only) | Not combined with evidence store |
| Citation quality gate | Loki (verification only) | No open system blocks publishing on citation failure |
| Auditable evidence map output | None | Evidence map methodology exists in social science but not automated |
| Full pipeline orchestration | None | No system ties all stages together |

---

## What This Means for Our Engine

The market validates the idea -- products like Perplexity and Elicit prove the demand, and QIAGEN proves the architecture works in production. But nothing exists as a reusable, product-agnostic package.

The closest path to building it would combine existing components:

- **Discovery**: Crawl4AI or Firecrawl behind an adapter interface, with a custom Source Policy layer on top
- **Evidence storage**: Custom, but informed by QIAGEN's triple-store pattern and the social science evidence map methodology
- **Synthesis**: STORM's outline-first approach is a good reference, adapted to draw strictly from stored evidence
- **Verification**: Loki's 5-step pipeline as the verifier agent, integrated into the quality gate
- **Orchestration**: LlamaIndex Workflows or LangGraph for the pipeline DAG
- **Writer/verifier pattern**: Novel-OS's agent separation model, adapted from fiction to evidence-based content

The novel contribution of our engine is the integration layer -- tying these stages into a single pipeline with enforced contracts between them, pluggable taxonomy/paths, and the "no citation, no ship" gate as a first-class architectural constraint rather than an optional add-on.
