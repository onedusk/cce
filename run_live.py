"""Live pipeline runner.

Usage:
    cd /path/to/cce
    PYTHONPATH=src python run_live.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Load .env manually (avoid adding python-dotenv as a dependency)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from cce.output import write_output
from cce.config.types import (
    CrawlConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    LLMConfig,
    QualityGateConfig,
)
from cce.discovery.adapters.firecrawl import FirecrawlAdapter
from cce.discovery.embeddings import EmbeddingUnavailableError
from cce.discovery.ollama import OllamaEmbeddingProvider
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.anthropic import AnthropicProvider
from cce.models.request import CurationRequest
from cce.orchestrator.pipeline import Pipeline
from cce.policy.loader import load_policy
from cce.tagging.loader import load_path_configs, load_taxonomy
from cce.tagging.wellbeing import WellBeingTaxonomy

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.runner")


async def main():
    # --- Config ---
    config = EngineConfig(
        llm=LLMConfig(
            provider="anthropic",
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            api_key=os.environ["ANTHROPIC_API_KEY"],
            temperature=0.2,
            max_tokens=16384,
        ),
        evidence_store=EvidenceStoreConfig(
            sqlite_path=Path("/tmp/cce_evidence_live.db"),
        ),
        crawl=CrawlConfig(
            adapter="firecrawl",
            api_key=os.environ["FIRECRAWL_API_KEY"],
            rate_limit_rps=2.0,
            timeout_seconds=30,
        ),
        quality_gate={
            "medium": QualityGateConfig(
                autopublish_threshold=0.7,
                min_citations_per_paragraph=1,
                min_citation_density_ratio=0.8,
                max_writer_iterations=3,
            ),
        },
    )

    # --- Load policy ---
    policy = load_policy("policies/peer-reviewed.yaml")
    logger.info("Loaded policy: %s", policy.name)

    # --- Build components ---
    evidence_store = SQLiteEvidenceStore(config.evidence_store)
    await evidence_store.connect()

    crawl_adapter = FirecrawlAdapter(config.crawl)
    llm = AnthropicProvider(config.llm)

    # --- Build embedding provider (optional — falls back to length-based ranking) ---
    embedding_provider = None
    if config.embedding.enabled:
        try:
            provider = OllamaEmbeddingProvider(config.embedding)
            await provider.embed(["health check"])
            embedding_provider = provider
            logger.info("Embedding provider ready: %s", config.embedding.model)
        except (EmbeddingUnavailableError, Exception) as e:
            logger.warning("Embedding provider unavailable, using length-based ranking: %s", e)

    # --- Build taxonomy plugin (optional — falls back to untagged evidence) ---
    taxonomy_plugin = None
    taxonomy_id = None
    taxonomy_path = Path("taxonomies/wellbeing-8d.yaml")
    if taxonomy_path.exists():
        try:
            taxonomy_config = load_taxonomy(taxonomy_path)
            taxonomy_plugin = WellBeingTaxonomy(taxonomy_config)
            taxonomy_id = taxonomy_config.id
            logger.info("Taxonomy loaded: %s (%d dimensions)", taxonomy_config.name, len(taxonomy_config.dimensions))
        except Exception as e:
            logger.warning("Failed to load taxonomy: %s", e)

    # --- Load path configs (optional) ---
    path_configs = None
    path_config_id = None
    path_configs_path = Path("path_configs/thnklabs.yaml")
    if path_configs_path.exists():
        try:
            path_configs = load_path_configs(path_configs_path)
            path_config_id = "thnklabs"
            logger.info("Path configs loaded: %s", list(path_configs.keys()))
        except Exception as e:
            logger.warning("Failed to load path configs: %s", e)

    pipeline = Pipeline(
        config=config,
        crawl_adapter=crawl_adapter,
        evidence_store=evidence_store,
        llm=llm,
        embedding_provider=embedding_provider,
        taxonomy_plugin=taxonomy_plugin,
        path_configs=path_configs,
    )

    # --- Build request ---
    request = CurationRequest(
        topic="the science and physiology of stress",
        subtopics=["acute vs chronic stress", "allostatic load and HPA axis", "eustress and hormetic stress", "stress recovery and inoculation"],
        paths=["learn", "explore", "apply"],
        audience="general",
        policy_id="peer-reviewed",
        taxonomy_id=taxonomy_id,
        path_config_id=path_config_id,
        risk_profile="medium",
    )

    logger.info("Starting pipeline for: %s", request.topic)
    logger.info("Paths: %s", request.paths)
    logger.info("Audience: %s", request.audience)

    # --- Run ---
    result = await pipeline.run(request, policy)

    # --- Write output ---
    output_dir = Path(__file__).parent / "output"
    run_dir = write_output(result, output_dir)
    logger.info("Output written to: %s", run_dir)

    # --- Console summary ---
    print("\n" + "=" * 70)
    print("PIPELINE RESULT")
    print("=" * 70)
    print(f"Status:    {result.job.status.value}")
    print(f"Job ID:    {result.job.id}")

    if result.job.error:
        print(f"Error:     {result.job.error.message}")

    if result.package:
        pkg = result.package
        print(f"Run ID:    {pkg.lineage.run_id}")
        print(f"Evidence:  {len(pkg.evidence)} objects")
        print(f"Units:     {len(pkg.units)}")
        print(f"Scores:    confidence={pkg.scores.confidence}, coverage={pkg.scores.coverage}, diversity={pkg.scores.source_diversity}")

    print(f"\nGate results: {len(result.gate_results)}")
    for gr in result.gate_results:
        print(f"  Iteration {gr.iteration}: {gr.decision.value} (confidence={gr.confidence})")
        if gr.feedback and gr.decision.value != "pass":
            print(f"    Feedback: {gr.feedback[:200]}")

    total = await evidence_store.count()
    print(f"\nEvidence store total: {total}")
    print(f"\nFull output: {run_dir}/")
    print(f"  result.json        — complete pipeline result")
    print(f"  content.md         — rendered content")
    print(f"  evidence.json      — {len(pkg.evidence) if result.package else 0} evidence objects")
    print(f"  verification.json  — gate results & verification reports")

    await evidence_store.close()


if __name__ == "__main__":
    asyncio.run(main())
