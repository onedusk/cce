"""Live pipeline runner with the full humanization stack enabled.

Loads config/humanization_live.yaml so the scorer / editor / implied-claim
checker are all active, then iterates the TOPICS list below. One job per
topic so operators can read end-to-end log output per topic and compare
baselines (output/mdx/_baseline/<slug>/) against the humanized re-runs
(output/mdx/<slug>/) side-by-side.

Usage:
    PYTHONPATH=src uv run python scripts/run_live_humanization.py
    # logs to stdout + writes raw output + MDX to output/

After the run completes, re-score the published MDX to compare WRITER scores
(visible in JobStage.SCORE records, captured below) vs FINAL scores (after
the editor's rewrite):
    uv run python scripts/run_score_sweep.py --dir output/mdx
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load .env (mirrors run_batch.py pattern)
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from cce.engine import CurationEngine  # noqa: E402
from cce.models.job import JobStage  # noqa: E402
from cce.models.request import CurationRequest  # noqa: E402
from cce.output import write_output  # noqa: E402
from cce.output.mdx import emit_mdx  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cce.live_humanization")


# --- Topics ---
# Each topic phrasing matches its existing slug so emit_mdx overwrites the same
# directory. Pre-run, copy output/mdx/<slug>/ into output/mdx/_baseline/<slug>/
# so the baseline stays intact for side-by-side comparison. Subtopics align
# with the research angles in docs/internal/thnklabs-topic-queue.md.

TOPICS: list[dict[str, object]] = [
    {
        "topic": "the science and physiology of stress",
        "subtopics": [
            "acute vs chronic stress",
            "allostatic load and HPA axis",
            "eustress and hormetic stress",
            "stress recovery and inoculation",
        ],
    },
    {
        "topic": "burnout: causes, mechanisms, and recovery",
        "subtopics": [
            "Maslach burnout dimensions: exhaustion, cynicism, inefficacy",
            "organizational vs individual causes of burnout",
            "WHO ICD-11 classification and workplace burnout",
            "recovery trajectories and intervention trials",
            "presenteeism and the hidden costs of burnout",
        ],
    },
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
]


def _summarize_humanization_stages(job) -> None:
    """Print a per-path summary of SCORE / EDIT records and the metrics
    they captured. Operator-facing: shows whether the editor fired,
    whether citations were preserved, and the writer-side StyleScores
    that gated each editor invocation.
    """
    score_records = [r for r in job.stages if r.stage == JobStage.SCORE]
    edit_records = [r for r in job.stages if r.stage == JobStage.EDIT]

    print("\n" + "=" * 70)
    print("HUMANIZATION STAGE SUMMARY")
    print("=" * 70)
    print(
        f"SCORE records: {len(score_records)} "
        f"(one per write iteration per path)"
    )
    print(f"EDIT records:  {len(edit_records)} (editor invocations)")

    if not score_records:
        print("\nNo SCORE records — humanization scorer did not run.")
        return

    print("\nPer-path SCORE detail (writer's draft, pre-editor):")
    print(
        f"  {'path':<10} {'stddev':>6} {'ttr':>5} "
        f"{'vocab':>5} {'contr':>5} {'hedge':>5} {'emdash':>6} {'pass':>5}"
    )
    for rec in score_records:
        m = rec.metrics or {}
        print(
            f"  {m.get('path', '?'):<10} "
            f"{m.get('sentence_length_stddev', 0):>6.2f} "
            f"{m.get('type_token_ratio', 0):>5.3f} "
            f"{m.get('suppressed_vocab_hits', 0):>5} "
            f"{m.get('contrastive_frame_count', 0):>5} "
            f"{m.get('hedging_phrase_count', 0):>5} "
            f"{m.get('em_dash_count', 0):>6} "
            f"{'YES' if m.get('humanization_pass') else 'NO':>5}"
        )

    if edit_records:
        print("\nPer-invocation EDIT detail:")
        print(
            f"  {'path':<10} {'preserved':>10} "
            f"{'words_in':>9} {'words_out':>10} "
            f"{'tok_in':>8} {'tok_out':>8}"
        )
        for rec in edit_records:
            m = rec.metrics or {}
            print(
                f"  {m.get('path', '?'):<10} "
                f"{'YES' if m.get('citations_preserved') else 'NO':>10} "
                f"{m.get('word_count_before', 0):>9} "
                f"{m.get('word_count_after', 0):>10} "
                f"{m.get('tokens_input', 0):>8} "
                f"{m.get('tokens_output', 0):>8}"
            )


async def _run_one(engine: CurationEngine, topic_config: dict[str, object]) -> None:
    topic = str(topic_config["topic"])
    subtopics = list(topic_config["subtopics"])  # type: ignore[arg-type]

    logger.info("=" * 70)
    logger.info("LIVE HUMANIZATION RUN: %s", topic)
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
    job = await handle.wait(timeout=2400)  # 40 minute ceiling per topic
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

    print("\n" + "=" * 70)
    print(f"LIVE HUMANIZATION RUN: {topic}")
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
        for unit in package.units:
            style = unit.style_scores
            if style is not None:
                print(
                    f"  {unit.path}: style_pass={style.humanization_pass}  "
                    f"em_dashes={style.em_dash_count}  "
                    f"vocab={style.suppressed_vocab_hits}"
                )

    _summarize_humanization_stages(job)


async def main() -> None:
    config_path = ROOT / "config" / "humanization_live.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"humanization config not found at {config_path}"
        )

    logger.info("Loading engine with humanization config: %s", config_path)
    engine = await CurationEngine.embedded(config_path=str(config_path))

    try:
        for i, topic_config in enumerate(TOPICS, start=1):
            logger.info("=" * 70)
            logger.info("TOPIC %d/%d", i, len(TOPICS))
            logger.info("=" * 70)
            try:
                await _run_one(engine, topic_config)
            except Exception as e:
                logger.error(
                    "TOPIC %d/%d FAILED (%s): %s",
                    i,
                    len(TOPICS),
                    topic_config.get("topic"),
                    e,
                    exc_info=True,
                )
                continue

        logger.info("=" * 70)
        logger.info("BATCH COMPLETE: %d topics processed", len(TOPICS))
        logger.info("=" * 70)

    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
