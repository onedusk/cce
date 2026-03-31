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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.batch")

# --- Topics to run ---
TOPICS = [
    {
        "topic": "the science of attention and focus",
        "subtopics": [
            "attention economy and cognitive switching costs",
            "deep work and sustained focus",
            "attention restoration theory and nature exposure",
            "neuroplasticity of attention",
        ],
    },
    {
        "topic": "burnout — causes, mechanisms, and recovery",
        "subtopics": [
            "Maslach burnout dimensions: exhaustion, cynicism, inefficacy",
            "organizational vs individual causes of burnout",
            "recovery trajectories and intervention research",
            "presenteeism and the cost of working while burned out",
        ],
    },
    {
        "topic": "loneliness, social isolation, and health",
        "subtopics": [
            "epidemic of disconnection and modern loneliness",
            "health effects of social isolation: mortality, immune function",
            "loneliness vs solitude — when being alone is harmful vs restorative",
            "digital connection as substitute vs supplement for in-person contact",
        ],
    },
    {
        "topic": "how environment design shapes well-being",
        "subtopics": [
            "environmental psychology and biophilic design",
            "noise, light, and temperature effects on cognition and mood",
            "workspace design and productivity",
            "urban vs rural well-being and restorative environments",
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
                job = await handle.wait()
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
