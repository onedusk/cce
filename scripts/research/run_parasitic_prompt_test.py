"""One-shot humanization run limited to the 3 parasitic-prompt-test topics.

What a "parasitic prompt test" is: a *parasitic* contrastive frame is the
AI-fingerprint pattern where a draft dismisses a topic as a degraded form of
something else ("X is not A. It is B") instead of presenting a genuine
alternative — subtype definitions and patterns live in the sibling
``run_contrastive_census.py``. This script is the *prompt* half of that
diagnostic (Diagnostic 3 of the Phase A/B scoping): it re-runs the three
topics that produced the most parasitic frames with a learn-path
``prompt_addendum`` change, to measure whether instructing the writer up
front reduces parasitic-frame production at generation time, versus
repairing it post-hoc in the Editor.

Keeps the main ``run_live_humanization.py`` untouched so the main runner's
topic list isn't clobbered during the experiment.

Live run: costs real LLM and crawl spend; expects API keys in ``.env``.

Usage:
    uv run python scripts/research/run_parasitic_prompt_test.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from cce.engine import CurationEngine  # noqa: E402
from cce.models.request import CurationRequest  # noqa: E402
from cce.output import write_output  # noqa: E402
from cce.output.mdx import emit_mdx  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.parasitic_test")


# Same topic phrasings / subtopics as the prior humanization batch so the
# slugs collide and emit_mdx overwrites the current humanized output at
# output/mdx/<slug>/ — the "after" state for the census comparison.
# Pre-run snapshot lives at output/mdx_parasitic_test/before/.

TOPICS: list[dict[str, object]] = [
    {
        "topic": "curiosity: the antidote to certainty",
        "subtopics": [
            "epistemic curiosity and lifelong learning",
            "information gap theory (Loewenstein)",
            "Kashdan's curiosity and well-being research",
            "curiosity vs anxiety: both scan for novelty",
            "curiosity in aging and cognitive resilience",
        ],
    },
    {
        "topic": "boredom: the signal we have been taught to silence",
        "subtopics": [
            "boredom as creativity catalyst",
            "default mode network and mind-wandering",
            "overstimulation and dopamine baseline",
            "boredom tolerance in children and adults",
        ],
    },
    {
        "topic": "the science and physiology of stress",
        "subtopics": [
            "acute vs chronic stress",
            "allostatic load and HPA axis",
            "eustress and hormetic stress",
            "stress recovery and inoculation",
        ],
    },
]


async def _run_one(engine: CurationEngine, topic_config: dict[str, object]) -> None:
    topic = str(topic_config["topic"])
    subtopics = list(topic_config["subtopics"])  # type: ignore[arg-type]

    logger.info("=" * 70)
    logger.info("PARASITIC-PROMPT TEST: %s", topic)
    logger.info("=" * 70)

    request = CurationRequest(
        topic=topic,
        subtopics=subtopics,
        paths=["learn", "explore", "apply"],
        audience="general",
        policy_id="peer-reviewed",
        taxonomy_id="wellbeing-8d",
        path_config_id="thnklabs",
        risk_profile="medium",
    )

    handle = await engine.curate(request)
    job = await handle.wait(timeout=2400)
    package = await handle.package()

    if package:
        from cce.orchestrator.pipeline import PipelineResult

        result = PipelineResult(package=package, job=job, gate_results=[])
        output_dir = ROOT / "output"
        run_dir = write_output(result, output_dir)
        logger.info("Output written to: %s", run_dir)

        mdx_dir = output_dir / "mdx"
        mdx_dir.mkdir(exist_ok=True)
        mdx_result = emit_mdx(package, mdx_dir, topic_name=topic)
        logger.info("MDX emitted to: %s", mdx_result.target_dir)

    print(f"\n=== {topic} ===")
    print(f"Status: {job.status.value}  Job ID: {job.id}")
    if package:
        print(f"Units: {len(package.units)}  Evidence: {len(package.evidence)}")


async def main() -> None:
    config_path = ROOT / "config" / "humanization_live.yaml"
    logger.info("Loading engine with humanization config: %s", config_path)
    engine = await CurationEngine.embedded(config_path=str(config_path))

    try:
        for i, topic_config in enumerate(TOPICS, start=1):
            logger.info("TOPIC %d/%d", i, len(TOPICS))
            try:
                await _run_one(engine, topic_config)
            except Exception as e:
                logger.error("TOPIC %d FAILED: %s", i, e, exc_info=True)
                continue
        logger.info("BATCH COMPLETE: %d topics", len(TOPICS))
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
