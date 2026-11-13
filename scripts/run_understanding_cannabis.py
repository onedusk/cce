"""Live run for the thnklabs "Understanding Cannabis" study.

The `cce` CLI can't pass constraints (jurisdiction) or a path config, so
this driver builds the request directly.

Usage:
    cd /path/to/cce
    uv run python scripts/run_understanding_cannabis.py           # paid run
    uv run python scripts/run_understanding_cannabis.py --check   # config only

The default caps (5 excerpts per page, 100 total) dropped the key lines of
the CCA pages in the first run, and only capped excerpts are stored, so a
rerun against the same database reuses them. Run 2 used a fresh database:
    CCE_EVIDENCE_SQLITE_PATH=$PWD/evidence_cannabis.db \\
    CCE_CRAWL_MAX_PER_SOURCE=20 CCE_CRAWL_MAX_EVIDENCE=250 \\
    uv run python scripts/run_understanding_cannabis.py
Set the same CCE_EVIDENCE_SQLITE_PATH for `cce status` and `cce emit-mdx`.
"""

import asyncio
import logging
import sys
from pathlib import Path

from cce import load_env_file

ROOT = Path(__file__).resolve().parent.parent

load_env_file(ROOT / ".env")

from cce.engine import CurationEngine  # noqa: E402
from cce.models.request import CurationConstraints, CurationRequest  # noqa: E402
from cce.output import write_output  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.runner")

PATH_CONFIG = "path_configs/understanding-cannabis.yaml"

REQUEST = CurationRequest(
    # Slug derives from the topic: "understanding-cannabis".
    topic="Understanding Cannabis",
    subtopics=[
        "cannabis product label terms THC THCA total THC CBD explained",
        "total THC calculation THCA 0.877 conversion factor",
        "USDA hemp definition total THC federal change 2026",
        "state cannabis regulator how to read a certificate of analysis",
        "cannabis laboratory testing microbials heavy metals pesticides residual solvents mycotoxins",
        "indica sativa strain names reliability cannabinoid content research",
        "Virginia Cannabis Control Authority laws and regulations",
        "Virginia retail marijuana market 2027 licenses Cannabis Control Authority",
        "Virginia 2026 retail cannabis law General Assembly",
        "VDACS Virginia hemp products total THC per package limit",
        "Virginia marijuana possession home cultivation limits adults 21",
        "Virginia cannabis edibles serving package THC limits packaging labeling",
        "Virginia House Bill 30 2026 enrolled text retail marijuana tax edible limits",
    ],
    paths=["learn", "explore", "apply"],
    audience="general adult readers in Virginia",
    constraints=CurationConstraints(
        jurisdiction="Virginia, USA (state law as of October 2026) and US federal law",
    ),
    policy_id="cannabis-va",
    taxonomy_id="wellbeing-8d",
    path_config_id="understanding-cannabis",
    risk_profile="medium",
)


def check() -> None:
    """Print what embedded() will load, without starting a job."""
    from cce.config.registry import ConfigRegistry

    registry = ConfigRegistry.load(Path("."), path_configs_path=Path(PATH_CONFIG))
    for pid, path in registry.path_configs.items():
        print(f"path {pid}: {(path.prompt_addendum or '')[:70]}")
    policy = registry.policies[REQUEST.policy_id].resolve_for_topic(REQUEST.topic)
    print("policy allow:", policy.domains_allow, "cap:", policy.max_sources_per_run)


async def main() -> None:
    engine = await CurationEngine.embedded(path_configs_path=PATH_CONFIG)

    try:
        logger.info("Starting pipeline for: %s", REQUEST.topic)
        handle = await engine.curate(REQUEST)
        # June three-path jobs took 10-18 min before humanization was on by
        # default; a short wait cancels a paid run on close().
        job = await handle.wait(timeout=7200)

        package = await handle.package()
        run_dir = None
        if package:
            from cce.orchestrator.pipeline import PipelineResult

            result = PipelineResult(package=package, job=job, gate_results=[])
            run_dir = write_output(result, ROOT / "output")
            logger.info("Output written to: %s", run_dir)

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
            print(
                f"Scores:    confidence={package.scores.confidence}, "
                f"coverage={package.scores.coverage}, "
                f"diversity={package.scores.source_diversity}"
            )
            print(f"Output:    {run_dir}/")
    finally:
        await engine.close()


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        asyncio.run(main())
