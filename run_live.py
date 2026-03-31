"""Live pipeline runner.

Usage:
    cd /path/to/cce
    PYTHONPATH=src python run_live.py
"""

import asyncio
import logging
import os
from pathlib import Path

# Load .env manually (avoid adding python-dotenv as a dependency)
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

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.runner")


async def main():
    engine = await CurationEngine.embedded()

    try:
        request = CurationRequest(
            topic="the science and physiology of stress",
            subtopics=[
                "acute vs chronic stress",
                "allostatic load and HPA axis",
                "eustress and hormetic stress",
                "stress recovery and inoculation",
            ],
            paths=["learn", "explore", "apply"],
            audience="general",
            policy_id="peer-reviewed",
            taxonomy_id="wellbeing-8d",
            path_config_id="thnklabs",
            risk_profile="medium",
        )

        logger.info("Starting pipeline for: %s", request.topic)
        logger.info("Paths: %s", request.paths)
        logger.info("Audience: %s", request.audience)

        handle = await engine.curate(request)
        job = await handle.wait()

        # --- Write output ---
        package = await handle.package()
        if package:
            from cce.orchestrator.pipeline import PipelineResult

            # Reconstruct PipelineResult for write_output compatibility
            result = PipelineResult(
                package=package,
                job=job,
                gate_results=[],
            )
            output_dir = Path(__file__).parent / "output"
            run_dir = write_output(result, output_dir)
            logger.info("Output written to: %s", run_dir)

        # --- Console summary ---
        print("\n" + "=" * 70)
        print("PIPELINE RESULT")
        print("=" * 70)
        print(f"Status:    {job.status.value}")
        print(f"Job ID:    {job.id}")

        if job.error:
            print(f"Error:     {job.error.message}")

        if package:
            print(f"Evidence:  {len(package.evidence)} objects")
            print(f"Units:     {len(package.units)}")
            print(f"Scores:    confidence={package.scores.confidence}, coverage={package.scores.coverage}, diversity={package.scores.source_diversity}")
            print(f"\nFull output: {run_dir}/")

    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
