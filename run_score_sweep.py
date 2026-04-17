"""Score every existing MDX page against the humanization thresholds.

Calibration script (humanization M01-M04 follow-up). Walks
``output/mdx/*/*/page.mdx``, extracts the JSON-embedded "content" field,
strips the ``[^N]`` footnote references that the MDX emitter produces, and
runs the programmatic Scorer on each draft.

Output:
- Per-file table: topic × path × key metrics × pass/fail + the flagged threshold.
- Aggregate: overall pass rate, per-metric fail rate, per-path fail rate.
- Percentile distribution per metric — the input for any threshold retune.

Cost: $0. No network, no LLM. Pure Python text analysis.

Usage:
    uv run python run_score_sweep.py
    uv run python run_score_sweep.py --dir output/mdx    # override default
    uv run python run_score_sweep.py --csv scores.csv    # also emit CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from cce.config.markers import load_markers
from cce.config.types import HumanizationThresholds
from cce.models.style import StyleScores
from cce.synthesis.scoring import Scorer

# MDX emitter wraps citations as [^1], [^2], etc. Some older runs emit
# [^?] placeholders when citation resolution failed. Strip both before
# scoring so they don't inflate word count or distort lexical diversity.
_FOOTNOTE_RE = re.compile(r"\[\^(?:\d+|\?)\]")

# Find the start of the "content": "..." field inside a JSON block.
_CONTENT_KEY_RE = re.compile(r'"content"\s*:\s*"')


def _extract_json_string_escape_aware(text: str, start_idx: int) -> str | None:
    """Read a JSON string literal starting at `start_idx` (the char after
    the opening quote). Returns the decoded string, or None on truncation.

    Handles JSON escape sequences (\\", \\\\, \\n, etc.) via json.loads on
    the assembled literal.
    """
    i = start_idx
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2  # skip the escape + escaped char
            continue
        if c == '"':
            literal = text[start_idx - 1 : i + 1]  # re-include the quotes
            try:
                return json.loads(literal)
            except json.JSONDecodeError:
                return None
        i += 1
    return None  # truncated before closing quote


def extract_body(mdx_path: Path) -> str | None:
    """Return the prose body of an MDX page, handling both emit formats.

    The emitter has two historical formats, both seen in output/mdx:
      1. JSON-wrapped: metadata block, then ```json { "content": "...", ... }
         (sometimes truncated — JSON may not close). We extract the content
         field via an escape-aware scanner.
      2. Direct markdown: metadata block, then raw markdown body with
         [^N] footnote refs inline.

    In either case, returns the body with footnote refs stripped so the
    scorer's word/sentence math isn't perturbed by citation notation.
    """
    raw = mdx_path.read_text()

    # Try format 1: extract "content" field out of the JSON-wrapped block.
    key_match = _CONTENT_KEY_RE.search(raw)
    if key_match is not None:
        start = key_match.end()  # first char inside the string
        body = _extract_json_string_escape_aware(raw, start)
        if body is not None:
            return _FOOTNOTE_RE.sub("", body)
        # else: JSON truncated — fall through to raw-markdown extraction

    # Format 2: skip the JS metadata block, return the rest as markdown.
    # The metadata block is a `export const metadata = { ... };` header
    # closing with `}` (or `};`) at column 0 on its own line.
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped == "}" or stripped == "};":
            # Everything after this line is the body
            body = "\n".join(lines[i + 1 :]).strip()
            if body:
                return _FOOTNOTE_RE.sub("", body)
            return None
    return None


def score_page(scorer: Scorer, mdx_path: Path) -> StyleScores | None:
    body = extract_body(mdx_path)
    if body is None:
        return None
    return scorer.score(body)


def failed_metrics(
    scores: StyleScores, thresholds: HumanizationThresholds
) -> list[str]:
    """Return the threshold names that tripped on this draft."""
    flags: list[str] = []
    if scores.sentence_length_stddev < thresholds.min_sentence_length_stddev:
        flags.append("stddev")
    if (
        scores.density_per_1000(scores.suppressed_vocab_hits)
        > thresholds.max_suppressed_vocab_hits_per_1000
    ):
        flags.append("vocab")
    if scores.type_token_ratio < thresholds.min_type_token_ratio:
        flags.append("ttr")
    if (
        scores.density_per_1000(scores.formulaic_transition_count)
        > thresholds.max_formulaic_transitions_per_1000
    ):
        flags.append("transitions")
    if (
        scores.density_per_1000(scores.contrastive_frame_count)
        > thresholds.max_contrastive_frames_per_1000
    ):
        flags.append("contrastive")
    if (
        scores.density_per_1000(scores.hedging_phrase_count)
        > thresholds.max_hedging_density_per_1000
    ):
        flags.append("hedging")
    return flags


def format_row(topic: str, path: str, scores: StyleScores, flags: list[str]) -> str:
    mark = "PASS" if scores.humanization_pass else "FAIL"
    flagstr = ",".join(flags) if flags else "-"
    return (
        f"{mark}  {topic:<60.60}  {path:<8}  "
        f"words={scores.word_count:>5}  stddev={scores.sentence_length_stddev:>5.2f}  "
        f"ttr={scores.type_token_ratio:.3f}  "
        f"vocab={scores.suppressed_vocab_hits:>3}  "
        f"trans={scores.formulaic_transition_count:>2}  "
        f"contr={scores.contrastive_frame_count:>2}  "
        f"hedge={scores.hedging_phrase_count:>2}  "
        f"fail={flagstr}"
    )


def print_percentiles(name: str, values: list[float]) -> None:
    if not values:
        print(f"  {name:<35}  (no data)")
        return
    sorted_vals = sorted(values)

    def pct(p: float) -> float:
        return (
            statistics.quantiles(sorted_vals, n=100, method="inclusive")[int(p) - 1]
            if len(sorted_vals) >= 2
            else sorted_vals[0]
        )

    print(
        f"  {name:<35}  min={sorted_vals[0]:>7.2f}  "
        f"p25={pct(25):>7.2f}  p50={pct(50):>7.2f}  "
        f"p75={pct(75):>7.2f}  max={sorted_vals[-1]:>7.2f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("output/mdx"),
        help="MDX output directory (default: output/mdx)",
    )
    parser.add_argument(
        "--markers",
        type=Path,
        default=Path("config/humanization_markers.yaml"),
        help="Marker YAML (default: config/humanization_markers.yaml)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV output path for external analysis",
    )
    args = parser.parse_args()

    if not args.dir.exists():
        print(f"error: {args.dir} not found", file=sys.stderr)
        return 2

    thresholds = HumanizationThresholds()
    markers = load_markers(args.markers)
    scorer = Scorer(thresholds=thresholds, markers=markers)

    mdx_files = sorted(args.dir.glob("*/*/page.mdx"))
    if not mdx_files:
        print(f"no page.mdx files under {args.dir}", file=sys.stderr)
        return 2

    results: list[dict] = []
    skipped: list[Path] = []

    print(f"Scoring {len(mdx_files)} MDX files against default thresholds:\n")
    print(f"  min_sentence_length_stddev     = {thresholds.min_sentence_length_stddev}")
    print(
        f"  max_suppressed_vocab_hits/1000 = "
        f"{thresholds.max_suppressed_vocab_hits_per_1000}"
    )
    print(f"  min_type_token_ratio           = {thresholds.min_type_token_ratio}")
    print(
        f"  max_formulaic_transitions/1000 = "
        f"{thresholds.max_formulaic_transitions_per_1000}"
    )
    print(
        f"  max_contrastive_frames/1000    = "
        f"{thresholds.max_contrastive_frames_per_1000}"
    )
    print(
        f"  max_hedging_density/1000       = {thresholds.max_hedging_density_per_1000}"
    )
    print()

    for mdx_path in mdx_files:
        topic = mdx_path.parent.parent.name
        path = mdx_path.parent.name
        scores = score_page(scorer, mdx_path)
        if scores is None:
            skipped.append(mdx_path)
            continue
        flags = failed_metrics(scores, thresholds)
        results.append({"topic": topic, "path": path, "scores": scores, "flags": flags})
        print(format_row(topic, path, scores, flags))

    if skipped:
        print(f"\nSkipped {len(skipped)} file(s) without JSON content block:")
        for p in skipped:
            print(f"  {p}")

    if not results:
        print("\nNo scorable files — nothing to aggregate.", file=sys.stderr)
        return 1

    # Aggregates
    total = len(results)
    passed = sum(1 for r in results if r["scores"].humanization_pass)
    print(f"\n=== Overall: {passed}/{total} passed ({100.0 * passed / total:.1f}%) ===")

    # Per-metric fail counts
    metric_fails: dict[str, int] = defaultdict(int)
    for r in results:
        for flag in r["flags"]:
            metric_fails[flag] += 1
    print("\nPer-metric fail counts (across all files):")
    for metric in (
        "stddev",
        "vocab",
        "ttr",
        "transitions",
        "contrastive",
        "hedging",
    ):
        count = metric_fails.get(metric, 0)
        pct = 100.0 * count / total if total else 0.0
        print(f"  {metric:<14}  {count:>3}/{total} ({pct:>5.1f}%)")

    # Per-path fail rate
    by_path: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_path[r["path"]].append(r["scores"].humanization_pass)
    print("\nPer-path pass rate:")
    for path, outcomes in sorted(by_path.items()):
        n = len(outcomes)
        p = sum(outcomes)
        print(f"  {path:<10}  {p}/{n} ({100.0 * p / n:.1f}%)")

    # Distributions for threshold calibration
    print("\nMetric distributions (for threshold calibration):")
    print_percentiles(
        "sentence_length_stddev",
        [r["scores"].sentence_length_stddev for r in results],
    )
    print_percentiles(
        "type_token_ratio",
        [r["scores"].type_token_ratio for r in results],
    )
    print_percentiles(
        "suppressed_vocab / 1000",
        [
            r["scores"].density_per_1000(r["scores"].suppressed_vocab_hits)
            for r in results
        ],
    )
    print_percentiles(
        "formulaic_transitions / 1000",
        [
            r["scores"].density_per_1000(r["scores"].formulaic_transition_count)
            for r in results
        ],
    )
    print_percentiles(
        "contrastive_frames / 1000",
        [
            r["scores"].density_per_1000(r["scores"].contrastive_frame_count)
            for r in results
        ],
    )
    print_percentiles(
        "hedging_phrases / 1000",
        [
            r["scores"].density_per_1000(r["scores"].hedging_phrase_count)
            for r in results
        ],
    )

    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "topic",
                    "path",
                    "humanization_pass",
                    "flagged",
                    "word_count",
                    "sentence_length_stddev",
                    "type_token_ratio",
                    "suppressed_vocab_hits",
                    "formulaic_transition_count",
                    "contrastive_frame_count",
                    "hedging_phrase_count",
                ]
            )
            for r in results:
                s: StyleScores = r["scores"]
                w.writerow(
                    [
                        r["topic"],
                        r["path"],
                        s.humanization_pass,
                        ",".join(r["flags"]),
                        s.word_count,
                        s.sentence_length_stddev,
                        s.type_token_ratio,
                        s.suppressed_vocab_hits,
                        s.formulaic_transition_count,
                        s.contrastive_frame_count,
                        s.hedging_phrase_count,
                    ]
                )
        print(f"\nCSV written to {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
