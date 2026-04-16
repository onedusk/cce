"""Batch pipeline runner — runs multiple topics sequentially.

Usage:
    PYTHONPATH=src uv run python run_batch.py
"""

import asyncio
import logging
import os
from pathlib import Path

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from cce.engine import CurationEngine
from cce.models.request import CurationRequest
from cce.output import write_output
from cce.output.mdx import emit_mdx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.batch")

# --- Topics to run ---
TOPICS = [
    {
        "topic": "resilience — post-traumatic growth and psychological flexibility",
        "subtopics": [
            "post-traumatic growth vs bouncing back",
            "psychological flexibility and ACT",
            "grit vs resilience distinction",
            "community resilience vs individual toughness",
        ],
    },
    {
        "topic": "boredom — the signal we have been taught to silence",
        "subtopics": [
            "boredom as creativity catalyst",
            "default mode network and mind-wandering",
            "overstimulation and dopamine baseline",
            "boredom tolerance in children and adults",
        ],
    },
]


async def main():
    engine = await CurationEngine.embedded()

    try:
        for i, topic_config in enumerate(TOPICS):
            topic = topic_config["topic"]
            logger.info("=" * 70)
            logger.info("BATCH RUN %d/%d: %s", i + 1, len(TOPICS), topic)
            logger.info("=" * 70)

            try:
                request = CurationRequest(
                    topic=topic,
                    subtopics=topic_config["subtopics"],
                    paths=["learn", "explore", "apply"],
                    audience="general",
                    policy_id="peer-reviewed",
                    taxonomy_id="wellbeing-8d",
                    path_config_id="thnklabs",
                    risk_profile="medium",
                )

                handle = await engine.curate(request)
                job = await handle.wait(timeout=1800)
                package = await handle.package()

                # Write output
                if package:
                    from cce.orchestrator.pipeline import PipelineResult

                    result = PipelineResult(
                        package=package, job=job, gate_results=[]
                    )
                    output_dir = Path(__file__).parent / "output"
                    run_dir = write_output(result, output_dir)
                    logger.info("Output written to: %s", run_dir)

                    # Also emit MDX
                    mdx_dir = output_dir / "mdx"
                    mdx_dir.mkdir(exist_ok=True)
                    mdx_result = emit_mdx(package, mdx_dir, topic_name=topic)
                    logger.info("MDX emitted to: %s", mdx_result.target_dir)

                # Console summary
                print("\n" + "=" * 70)
                print(f"BATCH RUN {i + 1}/{len(TOPICS)}: {topic}")
                print("=" * 70)
                print(f"Status:    {job.status.value}")
                print(f"Job ID:    {job.id}")

                if job.error:
                    print(f"Error:     {job.error.message}")

                if package:
                    print(f"Evidence:  {len(package.evidence)} objects")
                    print(f"Units:     {len(package.units)}")
                    print(
                        f"Scores:    confidence={package.scores.confidence}, "
                        f"coverage={package.scores.coverage}, "
                        f"diversity={package.scores.source_diversity}"
                    )

            except Exception as e:
                logger.error("BATCH RUN %d FAILED: %s", i + 1, e, exc_info=True)
                continue

        logger.info("=" * 70)
        logger.info("BATCH COMPLETE: %d topics processed", len(TOPICS))
        logger.info("=" * 70)

    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
