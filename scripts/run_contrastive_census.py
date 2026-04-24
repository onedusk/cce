"""Contrastive-frame subtype census over output/mdx/*/*/page.mdx.

Diagnostic for the Phase A vs Phase B decision in the humanization
contrastive-framing workstream. Does NOT modify production YAML or any
pipeline module. Runs locally-defined pattern sets against the corpus
and reports counts, densities, per-path breakdown, real examples, and
span-overlap events.

The script reuses ``extract_body`` from ``run_score_sweep.py`` so the
two tools stay consistent on MDX parsing. The scorer is invoked only
for ``word_count`` (per-1000 densities) — no threshold gating logic.

Usage:
    uv run python scripts/run_contrastive_census.py
    uv run python scripts/run_contrastive_census.py --dir output/mdx
    uv run python scripts/run_contrastive_census.py --examples 10
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from cce.config.markers import load_markers
from cce.config.types import HumanizationThresholds
from cce.synthesis.scoring import Scorer

# Import extract_body from the sibling script. Both scripts live in scripts/;
# adding this directory to sys.path lets us reuse the MDX extraction logic
# without duplicating its ~40 lines of JSON-wrapped / raw-markdown handling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_score_sweep import extract_body  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# -----------------------------------------------------------------------------
# Pattern sets — kept local, NOT written to YAML in this diagnostic pass.
# -----------------------------------------------------------------------------

# Current production patterns (as of 2026-04-22). Source of truth:
# config/humanization_markers.yaml contrastive_patterns.
# Subtype tag applied here for reporting — these are ALL currently
# treated as "genuine_alternative" by M04's counter-evidence model.
CURRENT_PATTERNS: list[tuple[str, str]] = [
    (r"\bunlike\s+[\w\s]+?,", "genuine_alternative"),
    (r"\bnot\s+(just|only)\s+[\w\s]+?,?\s+but\b", "genuine_alternative"),
    (r"\brather than\s+", "genuine_alternative"),
    (
        r"\b'?s not (about\s+)?[\w\s]+?,?\s+it'?s\s+(about\s+)?",
        "genuine_alternative",
    ),
]

# Proposed additions — parasitic subtype.
# "X is not A. It is B" (period-split) and "X is not A, it is B" (comma-split).
# Parasitic = Y is a degraded form of X rather than a true alternative.
PROPOSED_PARASITIC: list[tuple[str, str]] = [
    (
        r"\b(is|are|was|were)\s+not\s+[\w\s]+?\.\s+(It|They)\s+(is|are|was|were)\b",
        "parasitic",
    ),
    (
        r"\b(is|are)\s+not\s+[\w\s]+?,\s+(it|they)\s+(is|are)\b",
        "parasitic",
    ),
]

# Proposed additions — genuine_alternative expansion.
PROPOSED_GENUINE_ADDITIONS: list[tuple[str, str]] = [
    (r"\bby contrast\b", "genuine_alternative"),
]


@dataclass(frozen=True)
class Pattern:
    """One compiled regex with bookkeeping fields."""

    id: int
    regex: re.Pattern[str]
    source: str  # "current" | "proposed_parasitic" | "proposed_genuine_new"
    subtype: str  # "parasitic" | "genuine_alternative"
    raw: str


def _compile(
    entries: list[tuple[str, str]], start_id: int, source: str
) -> list[Pattern]:
    return [
        Pattern(
            id=start_id + i,
            regex=re.compile(raw, re.IGNORECASE),
            source=source,
            subtype=subtype,
            raw=raw,
        )
        for i, (raw, subtype) in enumerate(entries)
    ]


PATTERNS: list[Pattern] = (
    _compile(CURRENT_PATTERNS, start_id=0, source="current")
    + _compile(
        PROPOSED_PARASITIC, start_id=len(CURRENT_PATTERNS), source="proposed_parasitic"
    )
    + _compile(
        PROPOSED_GENUINE_ADDITIONS,
        start_id=len(CURRENT_PATTERNS) + len(PROPOSED_PARASITIC),
        source="proposed_genuine_new",
    )
)


# -----------------------------------------------------------------------------
# Per-draft scan
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    pattern_id: int
    source: str
    subtype: str
    text: str
    start: int
    end: int
    context: str  # ~120 chars around the match for reading


@dataclass
class DraftResult:
    path: Path
    topic_slug: str
    path_kind: str  # learn | explore | apply
    word_count: int
    matches: list[Match] = field(default_factory=list)

    @property
    def current_count(self) -> int:
        return sum(1 for m in self.matches if m.source == "current")

    @property
    def parasitic_count(self) -> int:
        return sum(1 for m in self.matches if m.source == "proposed_parasitic")

    @property
    def genuine_new_count(self) -> int:
        return sum(1 for m in self.matches if m.source == "proposed_genuine_new")

    @property
    def expanded_count(self) -> int:
        return len(self.matches)

    @property
    def delta(self) -> int:
        return self.expanded_count - self.current_count


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def _expand_to_sentence_pair(body: str, start: int, end: int) -> str:
    """Return the two-sentence window containing [start, end].

    Walks backward to the previous sentence terminator (or string start)
    and forward to the terminator after ``end`` plus one additional
    sentence to capture the 'It is B' half on period-split parasitic
    matches. Tolerates imperfect boundaries; falls back to a wide char
    window on failure.
    """
    # Walk back to the previous sentence terminator.
    prefix = body[:start]
    bound = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(prefix):
        bound = m.end()
    left = bound

    # Walk forward past the match end, then grab the next terminator + 1 more
    # sentence so period-split matches show both halves plus the sentence
    # that follows (gives the reader the "what came after" context).
    suffix = body[end:]
    terminators_to_consume = 2
    right = end
    for m in _SENTENCE_BOUNDARY_RE.finditer(suffix):
        terminators_to_consume -= 1
        right = end + m.start()
        if terminators_to_consume <= 0:
            break
    if terminators_to_consume > 0:  # end-of-body
        right = len(body)
    return body[left:right].strip().replace("\n", " ⏎ ")


def _scan_draft(path: Path, body: str, word_count: int) -> DraftResult:
    topic_slug = path.parent.parent.name
    path_kind = path.parent.name
    result = DraftResult(
        path=path,
        topic_slug=topic_slug,
        path_kind=path_kind,
        word_count=word_count,
    )
    for pat in PATTERNS:
        for m in pat.regex.finditer(body):
            start, end = m.span()
            ctx_start = max(0, start - 50)
            ctx_end = min(len(body), end + 70)
            context = body[ctx_start:ctx_end].replace("\n", " ⏎ ")
            result.matches.append(
                Match(
                    pattern_id=pat.id,
                    source=pat.source,
                    subtype=pat.subtype,
                    text=m.group(0),
                    start=start,
                    end=end,
                    context=context,
                )
            )
    # Attach the body so --dump-parasitic can pull wider context per match.
    result._body = body  # type: ignore[attr-defined]
    return result


def _count_overlaps(matches: list[Match]) -> int:
    """Count unordered pairs of matches with intersecting character spans."""
    sorted_matches = sorted(matches, key=lambda m: m.start)
    overlaps = 0
    for i in range(len(sorted_matches)):
        for j in range(i + 1, len(sorted_matches)):
            a, b = sorted_matches[i], sorted_matches[j]
            if b.start >= a.end:
                break  # sorted by start — no further can overlap with a
            if a.pattern_id == b.pattern_id:
                continue  # same pattern hitting twice is not overlap, just adjacency
            overlaps += 1
    return overlaps


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------


def _density(count: int, word_count: int) -> float:
    return (1000.0 * count / word_count) if word_count else 0.0


def _print_per_draft(results: list[DraftResult]) -> None:
    print(
        f"{'topic':<55}  {'path':<8}  {'words':>5}  "
        f"{'curr':>4}  {'para':>4}  {'gNew':>4}  {'total':>5}  "
        f"{'Δ':>3}  {'curr/1k':>7}  {'exp/1k':>6}"
    )
    print("-" * 120)
    for r in results:
        print(
            f"{r.topic_slug:<55.55}  {r.path_kind:<8}  {r.word_count:>5}  "
            f"{r.current_count:>4}  {r.parasitic_count:>4}  "
            f"{r.genuine_new_count:>4}  {r.expanded_count:>5}  "
            f"{r.delta:>3}  {_density(r.current_count, r.word_count):>7.2f}  "
            f"{_density(r.expanded_count, r.word_count):>6.2f}"
        )


def _print_aggregate(results: list[DraftResult]) -> None:
    total_words = sum(r.word_count for r in results)
    totals = {
        "current": sum(r.current_count for r in results),
        "parasitic": sum(r.parasitic_count for r in results),
        "genuine_new": sum(r.genuine_new_count for r in results),
    }
    expanded = totals["current"] + totals["parasitic"] + totals["genuine_new"]
    delta = totals["parasitic"] + totals["genuine_new"]

    print(f"\n=== Aggregate across {len(results)} drafts, {total_words:,} words ===")
    print(f"  current patterns (4)        : {totals['current']:>4}  "
          f"({_density(totals['current'], total_words):.2f}/1k)")
    print(f"  + proposed parasitic (2)    : {totals['parasitic']:>4}  "
          f"({_density(totals['parasitic'], total_words):.2f}/1k)")
    print(f"  + proposed genuine new (1)  : {totals['genuine_new']:>4}  "
          f"({_density(totals['genuine_new'], total_words):.2f}/1k)")
    print(f"  expanded total              : {expanded:>4}  "
          f"({_density(expanded, total_words):.2f}/1k)")
    print(f"  net delta from expansion    : +{delta} frames "
          f"({100.0 * delta / max(totals['current'], 1):.0f}% lift)")


def _print_per_path(results: list[DraftResult]) -> None:
    print("\n=== Per-path breakdown ===")
    by_path: dict[str, list[DraftResult]] = defaultdict(list)
    for r in results:
        by_path[r.path_kind].append(r)
    print(
        f"  {'path':<10}  {'drafts':>6}  {'words':>6}  "
        f"{'curr':>5}  {'para':>5}  {'gNew':>5}  "
        f"{'curr/1k':>7}  {'para/1k':>7}  {'gNew/1k':>7}"
    )
    for path_kind in ("learn", "explore", "apply"):
        rs = by_path.get(path_kind, [])
        if not rs:
            continue
        w = sum(r.word_count for r in rs)
        c = sum(r.current_count for r in rs)
        p = sum(r.parasitic_count for r in rs)
        g = sum(r.genuine_new_count for r in rs)
        print(
            f"  {path_kind:<10}  {len(rs):>6}  {w:>6}  "
            f"{c:>5}  {p:>5}  {g:>5}  "
            f"{_density(c, w):>7.2f}  {_density(p, w):>7.2f}  "
            f"{_density(g, w):>7.2f}"
        )


def _print_examples(
    results: list[DraftResult], per_source: int, out: argparse.Namespace | None = None
) -> None:
    print("\n=== Real examples (first "
          f"{per_source} per source) ===\n")
    by_source: dict[str, list[tuple[DraftResult, Match]]] = defaultdict(list)
    for r in results:
        for m in r.matches:
            by_source[m.source].append((r, m))

    for source in ("proposed_parasitic", "proposed_genuine_new", "current"):
        items = by_source.get(source, [])
        print(f"--- {source} ({len(items)} total matches) ---")
        if not items:
            print("    (no matches in corpus)\n")
            continue
        for r, m in items[:per_source]:
            print(
                f"  {r.topic_slug}/{r.path_kind}  "
                f"pattern_id={m.pattern_id} subtype={m.subtype}\n"
                f"    match: '{m.text.strip()}'\n"
                f"    ctx:   '…{m.context.strip()}…'"
            )
        print()


def _print_learn_distribution(results: list[DraftResult]) -> None:
    learn = [r for r in results if r.path_kind == "learn"]
    if not learn:
        return
    print("\n=== Per-article distribution, learn path only ===")
    print(
        f"  {'topic':<55}  {'words':>5}  {'para':>4}  {'para/1k':>7}  {'share_of_learn':>15}"
    )
    total_para = sum(r.parasitic_count for r in learn) or 1
    # Sort descending by parasitic count to surface concentration.
    for r in sorted(learn, key=lambda x: -x.parasitic_count):
        share = 100.0 * r.parasitic_count / total_para
        print(
            f"  {r.topic_slug:<55.55}  {r.word_count:>5}  "
            f"{r.parasitic_count:>4}  "
            f"{_density(r.parasitic_count, r.word_count):>7.2f}  "
            f"{share:>14.1f}%"
        )
    counts = [r.parasitic_count for r in learn]
    densities = [_density(r.parasitic_count, r.word_count) for r in learn]
    print(
        f"\n  learn stats (n={len(learn)}):"
        f"\n    parasitic count — mean={statistics.mean(counts):.2f}  "
        f"median={statistics.median(counts):.1f}  "
        f"stddev={statistics.pstdev(counts):.2f}  "
        f"max={max(counts)}"
        f"\n    parasitic/1k  — mean={statistics.mean(densities):.2f}  "
        f"median={statistics.median(densities):.2f}  "
        f"stddev={statistics.pstdev(densities):.2f}  "
        f"max={max(densities):.2f}"
    )
    # Concentration: top 1, top 3 share of total parasitic.
    sorted_counts = sorted(counts, reverse=True)
    top1_share = 100.0 * sorted_counts[0] / total_para
    top3_share = 100.0 * sum(sorted_counts[:3]) / total_para
    print(
        f"    concentration: top-1 article = {top1_share:.1f}%  "
        f"top-3 articles = {top3_share:.1f}%  of learn parasitic frames"
    )


def _dump_parasitic_review(results: list[DraftResult], out_path: Path) -> None:
    """Write every parasitic match with its two-sentence context to a review file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Parasitic-pattern match review")
    lines.append("")
    lines.append(
        "All matches from `proposed_parasitic` patterns across the corpus. "
        "Each entry shows the matched substring, the two-sentence window "
        "containing it, and the article source. Use this file to label each "
        "match as `parasitic` / `factual` / `ambiguous` and compute the FP rate."
    )
    lines.append("")

    # Flatten parasitic matches, sorted by topic/path for consistent review order.
    entries: list[tuple[DraftResult, Match]] = []
    for r in results:
        for m in r.matches:
            if m.source == "proposed_parasitic":
                entries.append((r, m))
    entries.sort(key=lambda pair: (pair[0].topic_slug, pair[0].path_kind, pair[1].start))

    lines.append(f"**Total parasitic matches: {len(entries)}**")
    lines.append("")
    lines.append("| # | topic / path | label | matched text | sentence window |")
    lines.append("|--:|---|---|---|---|")
    for i, (r, m) in enumerate(entries, start=1):
        body = getattr(r, "_body", "")
        window = _expand_to_sentence_pair(body, m.start, m.end)
        # Escape pipes for markdown table cells.
        matched = m.text.replace("|", "\\|").replace("\n", " ")
        window = window.replace("|", "\\|")
        lines.append(
            f"| {i} | `{r.topic_slug}` / `{r.path_kind}` | _unlabeled_ | "
            f"`{matched.strip()}` | {window} |"
        )

    lines.append("")
    lines.append(
        "## Labeling guide"
        "\n- `parasitic` — Y in 'X is not A. It is B' is a degraded / reframed form of A; "
        "the contrast is rhetorical scaffolding with no independent factual weight."
        "\n- `factual` — Y is a genuinely contrasting property; A and B stand as "
        "independent claims about X (e.g., 'the drug is not dangerous. It is essential')."
        "\n- `ambiguous` — the two-sentence window does not resolve which class the "
        "match belongs to without wider document context."
    )
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {len(entries)} parasitic matches to {out_path}")


def _print_overlaps(results: list[DraftResult]) -> None:
    print("=== Overlap events ===")
    total_overlaps = 0
    drafts_with_overlap = 0
    for r in results:
        n = _count_overlaps(r.matches)
        if n > 0:
            drafts_with_overlap += 1
            total_overlaps += n
    print(
        f"  drafts with ≥1 overlap : {drafts_with_overlap}/{len(results)}  "
        f"({100.0 * drafts_with_overlap / max(len(results), 1):.0f}%)"
    )
    print(f"  total overlap events  : {total_overlaps}")
    if total_overlaps == 0:
        print("  → subtype counts are span-disjoint on this corpus; dedup not needed.")
    else:
        print("  → dedup logic would be required before subtype counts can "
              "be used as independent metrics.")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=ROOT / "output" / "mdx",
        help="MDX corpus root (default: <repo>/output/mdx)",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=8,
        help="How many example matches to print per pattern source (default: 8)",
    )
    parser.add_argument(
        "--dump-parasitic",
        type=Path,
        default=None,
        help="Dump every parasitic match with sentence-pair context to this file.",
    )
    args = parser.parse_args()

    if not args.dir.exists():
        print(f"error: {args.dir} not found", file=sys.stderr)
        return 2

    # Instantiate the scorer purely for word_count (matches the pipeline's
    # citation-stripping before counting, so densities align with live runs).
    thresholds = HumanizationThresholds()
    markers = load_markers(ROOT / "config" / "humanization_markers.yaml")
    scorer = Scorer(thresholds=thresholds, markers=markers)

    # Skip _baseline/ and _metadata/ — these are archival copies, not the
    # current humanized corpus the Phase A/B decision is being made about.
    mdx_files = sorted(
        p for p in args.dir.glob("*/*/page.mdx") if not p.parent.parent.name.startswith("_")
    )
    if not mdx_files:
        print(f"no page.mdx files under {args.dir}", file=sys.stderr)
        return 2

    results: list[DraftResult] = []
    skipped: list[Path] = []

    for mdx_path in mdx_files:
        body = extract_body(mdx_path)
        if body is None:
            skipped.append(mdx_path)
            continue
        word_count = scorer.score(body).word_count
        results.append(_scan_draft(mdx_path, body, word_count))

    print(
        f"Scanned {len(results)} drafts "
        f"({len(skipped)} skipped as unparseable) "
        f"against {len(PATTERNS)} patterns "
        f"({len(CURRENT_PATTERNS)} current + {len(PROPOSED_PARASITIC)} parasitic "
        f"+ {len(PROPOSED_GENUINE_ADDITIONS)} genuine-new)\n"
    )

    _print_per_draft(results)
    _print_aggregate(results)
    _print_per_path(results)
    _print_learn_distribution(results)
    _print_examples(results, args.examples)
    _print_overlaps(results)

    if args.dump_parasitic is not None:
        _dump_parasitic_review(results, args.dump_parasitic)

    return 0


if __name__ == "__main__":
    sys.exit(main())
