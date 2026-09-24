"""Acceptance check for regenerated thnkLabs articles against the client's correction STYLE.

Structural/stylistic only — NEVER text or citation-count parity with
docs/internal/pages-converted (regeneration uses new sources). Two layers:

  (1) deterministic structural: no scaffolding headings; eight-dimensions in
      EXPLORE not LEARN; one citation entry per source URL.
  (2) semantic repetition (ADR-007 revision): ADVISORY only — an LLM-judge over
      the trio + an embedding near-duplicate claim signal, surfaced for human
      review and NOT part of the pass/fail gate. A lexical shingle
      overlap-coefficient survives only as a verbatim-copy tripwire.

Why the repetition check is NOT an automated gate (ADR-007 revision): a naive
k-word shingle metric was empirically rejected — on the loneliness trio the
client's *good* articles scored *higher* lexical overlap than the *bad* engine
output (k=4 overlap-coef learn↔explore: client 0.305 vs engine 0.192), because
the corrected articles are shorter and the engine's repetition is *reworded*.
But an LLM-judge cannot certify "minimal repetition" either: calibration
2026-06-23 (majority-of-3) had the judge FAIL the client's own gold-standard
trio 3/3. So cross-path repetition is a human-review step; the judge + embedding
signal are advisory inputs to that review, never the gate.

Embedding calibration (sim_threshold), measured 2026-06-23 with
``nomic-embed-text-v2-moe`` over sentence-level claims (>= 10 words, footnote
markers and markdown stripped):
  - chosen sim_threshold = 0.85
  - GOOD reference (docs/internal/pages-converted/loneliness-*.md, read as flat
    text): max cross-path cosine 0.946; 10 cross-path claim pairs >= 0.85.
  - BAD reference (output/mdx/loneliness-*/*/page.mdx via extract_body):
    max cross-path cosine 0.997; 40 cross-path claim pairs >= 0.85.
The bad trio's near-verbatim reworded restatements (e.g. an AHA-2022 statement
appearing at cosine 0.997 in both LEARN and EXPLORE) are exactly what the signal
flags; the bad trio carries ~4x the >= 0.85 cross-path pairs of the good one.
The embedding signal corroborates the judge; it is not the gate (ADR-007).

INPUT-SHAPE SPLIT (the trap): the structural layer reads emitted MDX
(metadata.citations + <topic>/<path>/page.mdx). The pages-converted references
are FLAT markdown with no metadata block and a prose References section, so
``extract_body`` (which requires a metadata block) returns None on them — read
the flat references with plain ``read_text`` for judge/embedding calibration.
At M04 there is no GOOD emitted-MDX reference, so the GOOD structural pass is
checkable only on REGENERATED output and is deferred to M05 (T-05.02); the
structural layer is validated here on the bad output/mdx trio (must flag
scaffolding) plus synthetic metadata.citations fixtures.

Usage:
    uv run python scripts/research/run_acceptance_check.py \
        --topic-dir output/mdx/loneliness-social-isolation-and-health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

# Import extract_body from the sibling script. Both scripts live in
# scripts/research/; adding this directory to sys.path lets us reuse the MDX
# extraction logic without duplicating its JSON-wrapped / raw-markdown handling
# (same pattern as run_contrastive_census.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_score_sweep import extract_body  # noqa: E402

from cce.discovery.discoverer import _cosine_similarity  # noqa: E402
from cce.discovery.embeddings import EmbeddingUnavailableError  # noqa: E402

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent

_PATHS = ("learn", "explore", "apply")

_SCAFFOLD_HEADINGS = {
    "overview",
    "introduction",
    "closing frame",
    "conclusion",
    "summary",
}

# The eight-dimensions framing belongs to EXPLORE (PDR-001). Matches both the
# client's "Eight Dimensions of Well-Being" heading and prose mentions.
_DIMENSION_RE = re.compile(
    r"eight dimensions|dimensions? of well[\s-]?being", re.IGNORECASE
)
# The 8-dimensions framing usually renders as one heading per dimension
# ("## Physical Well-Being", "## Emotional Well-Being", …) without ever writing
# the literal phrase "eight dimensions". These are the dimension words (plus
# common synonyms) to detect that heading pattern as a fallback.
_DIMENSION_WORDS = frozenset(
    {
        "physical",
        "emotional",
        "social",
        "intellectual",
        "cognitive",
        "environmental",
        "financial",
        "spiritual",
        "vocational",
        "occupational",
    }
)

# Footnote / evidence markers stripped before sentence-splitting claims so they
# do not perturb the embedding signal: [^1], [^?], [ev:ID], [ev_ID].
_MARKER_RE = re.compile(r"\[\^?(?:\d+|\?|ev[:_][^\]]+)\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


# --- (1) deterministic structural checks ---


def has_scaffolding_heading(body: str) -> list[str]:
    """Return any markdown headings whose text is a banned scaffolding label."""
    return [
        s
        for line in body.splitlines()
        if (s := line.strip()).startswith("#")
        and s.lstrip("#").strip().lower() in _SCAFFOLD_HEADINGS
    ]


def duplicate_url_citations(citations: list[dict]) -> dict[str, int]:
    """URLs appearing under >1 citation index in one article (empty post-M02)."""
    by_url: dict[str, set[int]] = {}
    for c in citations:
        by_url.setdefault(c["url"], set()).add(c["index"])
    return {u: len(ix) for u, ix in by_url.items() if len(ix) > 1}


_RESOURCES_HEADING_RE = re.compile(
    r"^#{2,3}[ \t]+.*resources.*$", re.IGNORECASE | re.MULTILINE
)


def resources_ungrounded(body: str) -> list[str]:
    """Resource bullets under a 'Resources' heading that lack a [^N] citation.

    Empty list = grounded (or no resources section). Non-empty means the writer
    recommended sources it cannot cite from the evidence (leakage). The thnkLabs
    emitter rebuilds this section deterministically from citations, so emitted
    pages are grounded by construction; this is the gate's safety net.
    """
    m = _RESOURCES_HEADING_RE.search(body)
    if m is None:
        return []
    rest = body[m.end() :]
    nxt = re.search(r"^#{2,3}[ \t]+", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    bullets = [
        ln.strip() for ln in section.splitlines() if ln.lstrip().startswith(("-", "*"))
    ]
    return [b[:80] for b in bullets if "[^" not in b]


def _raw_body(mdx_path: Path) -> str:
    """Body text after the metadata block, with [^N] markers INTACT.

    ``extract_body`` strips footnote refs (it serves prose analysis), so the
    resources-grounding check — which needs to see [^N] — reads the raw body
    instead: everything after the metadata's closing ``}``/``};`` line.
    """
    lines = mdx_path.read_text().splitlines()
    for i, ln in enumerate(lines):
        if ln.rstrip() in ("}", "};"):
            return "\n".join(lines[i + 1 :])
    return mdx_path.read_text()


def _dimensions_in_body(body: str) -> bool:
    """True if the body carries the eight-dimensions framing.

    Matches either the literal phrase ("eight dimensions"/"dimensions of
    well-being") OR the per-dimension heading pattern — >=5 distinct dimension
    words appearing in markdown headings (e.g. "## Physical Well-Being").
    """
    if _DIMENSION_RE.search(body):
        return True
    # Dimensions render either as markdown headings ("## Physical Well-Being")
    # or as bold inline labels ("**Physical health.**") in the terser survey
    # style — scan both kinds of label line.
    labels = [
        ln.lower() for ln in body.splitlines() if ln.lstrip().startswith(("#", "**"))
    ]
    hits = sum(1 for w in _DIMENSION_WORDS if any(w in h for h in labels))
    return hits >= 4


def dimensions_placement(topic_dir: Path | str) -> dict:
    """Check the eight-dimensions framing lives in EXPLORE, not LEARN (PDR-001).

    Thin file-reading shell over ``_dimensions_in_body``. Reads the EXPLORE and
    LEARN page.mdx bodies (via ``extract_body``); ``ok`` is True only when
    dimensions are present in EXPLORE and absent from LEARN.
    """
    topic_dir = Path(topic_dir)
    explore = extract_body(topic_dir / "explore" / "page.mdx")
    learn = extract_body(topic_dir / "learn" / "page.mdx")
    explore_has = _dimensions_in_body(explore) if explore else False
    learn_has = _dimensions_in_body(learn) if learn else False
    return {
        "explore_has_dimensions": explore_has,
        "learn_has_dimensions": learn_has,
        "ok": explore_has and not learn_has,
    }


# --- semantic repetition (ADR-007) ---


def _extract_claims(body: str, min_words: int = 10) -> list[str]:
    """Split a draft body into substantive claim sentences for the embedder.

    Drops headings, rules, and short fragments; strips footnote/evidence markers
    and markdown emphasis so boilerplate (shared titles, bold sub-heads) does not
    masquerade as a reworded restatement.
    """
    text = _MARKER_RE.sub("", body)
    claims: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        b = block.strip()
        if b.startswith("#") or b.startswith("---"):
            continue
        plain = re.sub(r"[*_`>]", "", b).strip()
        for sent in _SENTENCE_SPLIT_RE.split(plain):
            s = sent.strip()
            if len(s.split()) >= min_words:
                claims.append(s)
    return claims


async def embedding_near_duplicate_claims(
    claims_by_path: dict[str, list[str]],
    embedder,  # discovery.embeddings.EmbeddingProvider
    sim_threshold: float = 0.85,
) -> list[tuple[str, str, str, float]]:
    """Cross-path claim pairs whose cosine similarity exceeds the threshold.

    Reuses the existing embedding stack (discovery/embeddings.py +
    discoverer._cosine_similarity). Returns (path_a, path_b, "claim_a → claim_b"
    summary, score), sorted by descending score. Degrades to [] on
    EmbeddingUnavailableError so the judge layer still runs.
    """
    flat = [(p, c) for p, claims in claims_by_path.items() for c in claims]
    if len(flat) < 2:
        return []
    try:
        result = await embedder.embed([c for _, c in flat])
    except EmbeddingUnavailableError:
        logger.warning(
            "embedding_near_duplicate_claims: embeddings unavailable — "
            "skipping the corroborating signal (judge layer unaffected)."
        )
        return []
    vectors = result.vectors
    pairs: list[tuple[str, str, str, float]] = []
    for i in range(len(flat)):
        path_a, claim_a = flat[i]
        for j in range(i + 1, len(flat)):
            path_b, claim_b = flat[j]
            if path_a == path_b:
                continue
            score = _cosine_similarity(vectors[i], vectors[j])
            if score >= sim_threshold:
                pairs.append(
                    (path_a, path_b, f"{claim_a[:80]} → {claim_b[:80]}", score)
                )
    pairs.sort(key=lambda x: x[3], reverse=True)
    return pairs


# The judge rubric is a FIXED, inline string (ADR-007: at temperature 0 this
# makes the judge the deterministic authoritative gate). Editing it is a
# deliberate recalibration, not a per-run knob.
_JUDGE_RUBRIC = """You are auditing a trio of companion articles (LEARN, EXPLORE, APPLY) on a single
topic. They are meant to be read in sequence and should be ADDITIVE: each article
adds new framing, dimensions, or actions rather than re-explaining its siblings.

FAIL the trio if EXPLORE or APPLY RE-EXPLAINS a point already made in LEARN —
restating the same definition, statistic, or mechanism in reworded prose
(for example "The WHO estimates..." in LEARN and "The WHO has estimated..." in
EXPLORE). This is the defect the client objected to: "not by just rewording the
same sentence."

PASS the trio if EXPLORE and APPLY build on LEARN without re-explaining it.
Two things are NOT failures:
  - Citing the SAME source for a genuinely NEW point (the constraint is on
    repeated PROSE, not on shared citations).
  - Briefly REFERENCING a prior point to build on it (vs. RE-EXPLAINING it).

Respond with ONLY a JSON object, no prose around it:
{"verdict": "pass" | "fail",
 "offending_passages": ["<short quote from EXPLORE/APPLY that re-explains LEARN>", ...],
 "rationale": "<one or two sentences>"}
"""


def _parse_judge_response(text: str) -> dict:
    """Pull the JSON verdict object out of the judge's reply, defensively."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return {
            "verdict": "error",
            "offending_passages": [],
            "rationale": f"Could not locate JSON in judge response: {text[:200]!r}",
        }
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return {
            "verdict": "error",
            "offending_passages": [],
            "rationale": f"Judge response was not valid JSON: {e}",
        }
    return {
        "verdict": str(data.get("verdict", "error")).lower(),
        "offending_passages": list(data.get("offending_passages", [])),
        "rationale": str(data.get("rationale", "")),
    }


async def judge_repetition(drafts: dict[str, str], llm) -> dict:
    """LLM-judge: does EXPLORE/APPLY re-EXPLAIN points already made in LEARN?

    ``drafts`` is path → plain text (works for both the flat pages-converted
    references and extract_body-parsed MDX). This is the AUTHORITATIVE repetition
    gate (ADR-007); the embedding signal is corroborating. Called at temperature
    0 with the fixed ``_JUDGE_RUBRIC`` so the verdict is reproducible.

    Returns {verdict: "pass"|"fail"|..., offending_passages: [...], rationale}.
    If no LLM is configured, returns a logged SKIPPED result rather than raising.
    """
    if llm is None:
        logger.warning("judge_repetition: no LLM configured — returning SKIPPED.")
        return {
            "verdict": "skipped",
            "offending_passages": [],
            "rationale": "No LLM configured; semantic repetition judge skipped.",
        }

    # Import lazily so structural-only / test imports don't pull the LLM types.
    from cce.llm.base import LLMMessage

    ordered = [r for r in _PATHS if r in drafts]
    ordered += [k for k in drafts if k not in _PATHS]
    sections = [f"=== {role.upper()} ARTICLE ===\n{drafts[role]}" for role in ordered]
    user_prompt = (
        "\n\n".join(sections)
        + "\n\nAudit this trio against the rubric. Return only the JSON object."
    )
    response = await llm.complete(
        [LLMMessage(role="user", content=user_prompt)],
        temperature=0.0,
        system=_JUDGE_RUBRIC,
    )
    return _parse_judge_response(response.content)


# --- verbatim tripwire only (NOT the gate — see ADR-007) ---


def _shingles(text: str, k: int = 5) -> set[str]:
    w = re.findall(r"\w+", text.lower())
    return {" ".join(w[i : i + k]) for i in range(len(w) - k + 1)}


def verbatim_copy_tripwire(
    drafts: dict[str, str], k: int = 5, coef: float = 0.5
) -> list[tuple]:
    """Flag only EGREGIOUS verbatim copy (overlap-coefficient above a high bar).

    Diagnostic, not pass/fail — lexical overlap empirically cannot certify
    "minimal repetition" (ADR-007), so it survives only to catch wholesale
    copy-paste. The client trio sits far below the 0.5 bar (max 0.30).
    """
    sh = {p: _shingles(t, k) for p, t in drafts.items()}
    ps = list(sh)
    return [
        (a, b, len(sh[a] & sh[b]) / (min(len(sh[a]), len(sh[b])) or 1))
        for i, a in enumerate(ps)
        for b in ps[i + 1 :]
        if len(sh[a] & sh[b]) / (min(len(sh[a]), len(sh[b])) or 1) >= coef
    ]


# --- MDX reading + the top-level report ---


def _read_citations(mdx_path: Path) -> list[dict]:
    """Parse metadata.citations out of an emitted page.mdx.

    The emitter writes ``export const metadata = { ... };`` whose object closes
    with a line that is exactly ``}`` (or ``};``) at column 0 — the same anchor
    extract_body uses for the body. Returns [] when the block is absent/invalid.
    """
    lines = mdx_path.read_text().splitlines()
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.strip().startswith("export const metadata")
        ),
        None,
    )
    if start is None:
        return []
    for j in range(start, len(lines)):
        if lines[j].rstrip() in ("}", "};"):
            block = "\n".join(lines[start : j + 1])
            obj = block[block.index("{") : block.rindex("}") + 1]
            try:
                return json.loads(obj).get("citations", []) or []
            except json.JSONDecodeError:
                return []
    return []


def load_text_trio(
    md_dir: Path | str, slug_prefix: str = "loneliness"
) -> dict[str, str]:
    """Build a path → plain-text drafts dict from FLAT pages-converted markdown.

    For judge/embedding CALIBRATION only — these references have no metadata
    block, so they are read with plain ``read_text`` (extract_body would return
    None). Not used by the structural layer.
    """
    md_dir = Path(md_dir)
    drafts: dict[str, str] = {}
    for role in _PATHS:
        f = md_dir / f"{slug_prefix}-{role}.md"
        if f.exists():
            drafts[role] = f.read_text()
    return drafts


async def run_acceptance_check(topic_dir: Path | str, embedder=None, llm=None) -> dict:
    """Structured acceptance report for one emitted-MDX topic directory.

    Reports scaffolding hits, dimensions placement, duplicate-URL citations, the
    judge verdict, embedding near-duplicate pairs, and verbatim-tripwire hits.

    Gate is DETERMINISTIC only: no scaffolding AND dimensions-in-EXPLORE AND no
    duplicate-URL citations. These encode three of the client's four concrete
    asks. The fourth ("don't repeat across paths") is inherently a judgment call
    — the client's own gold-standard articles do not pass an automated judge of
    it (calibration 2026-06-23), and the client reviews this content anyway — so
    the LLM-judge + embedding signal are reported as ADVISORY for that human
    review, never as part of the pass/fail gate (ADR-007 revision).
    """
    topic_dir = Path(topic_dir)
    bodies: dict[str, str] = {}
    raw_bodies: dict[str, str] = {}
    citations: dict[str, list[dict]] = {}
    for role in _PATHS:
        mdx = topic_dir / role / "page.mdx"
        if mdx.exists():
            bodies[role] = extract_body(mdx) or ""
            raw_bodies[role] = _raw_body(mdx)
            citations[role] = _read_citations(mdx)

    scaffolding = {r: has_scaffolding_heading(b) for r, b in bodies.items()}
    dims = dimensions_placement(topic_dir)
    dup_urls = {r: duplicate_url_citations(c) for r, c in citations.items()}
    # resources_ungrounded needs [^N] intact -> use raw_bodies (not stripped).
    ungrounded_resources = {r: resources_ungrounded(b) for r, b in raw_bodies.items()}

    judge = await judge_repetition(dict(bodies), llm)
    embedding_pairs: list[tuple[str, str, str, float]] = []
    if embedder is not None:
        claims_by_path = {r: _extract_claims(b) for r, b in bodies.items()}
        embedding_pairs = await embedding_near_duplicate_claims(
            claims_by_path, embedder
        )
    tripwire = verbatim_copy_tripwire(dict(bodies))

    # Deterministic gate only — judge/embedding are advisory (see docstring).
    gate_pass = (
        not any(scaffolding.values())
        and dims["ok"]
        and not any(dup_urls.values())
        and not any(ungrounded_resources.values())
    )

    return {
        "topic_dir": str(topic_dir),
        "scaffolding": scaffolding,
        "dimensions": dims,
        "duplicate_url_citations": dup_urls,
        "ungrounded_resources": ungrounded_resources,
        "judge": judge,
        "embedding_near_duplicate_pairs": embedding_pairs,
        "verbatim_tripwire": tripwire,
        "gate_pass": gate_pass,
    }


async def _amain(args: argparse.Namespace) -> int:
    # Operator convenience: load .env so a configured ANTHROPIC_API_KEY activates
    # the judge. Without a key the run degrades cleanly to judge=SKIPPED.
    from cce import load_env_file
    from cce.config.loader import load_config

    load_env_file()
    config = load_config()

    embedder = None
    llm = None
    if config.embedding.enabled:
        try:
            from cce.discovery.ollama import OllamaEmbeddingProvider

            embedder = OllamaEmbeddingProvider(config.embedding)
        except Exception as e:  # noqa: BLE001 - QA script, degrade to judge-only
            logger.warning("Embedding provider unavailable (%s); skipping signal.", e)
    if config.llm.api_key:
        from cce.llm.anthropic import AnthropicProvider

        llm = AnthropicProvider(config.llm)
    else:
        logger.warning(
            "No LLM API key configured; the repetition judge will be SKIPPED."
        )

    report = await run_acceptance_check(args.topic_dir, embedder=embedder, llm=llm)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["gate_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic-dir",
        type=Path,
        required=True,
        help="Emitted-MDX topic directory (<topic>/<path>/page.mdx).",
    )
    args = parser.parse_args()
    if not args.topic_dir.exists():
        print(f"error: {args.topic_dir} not found", file=sys.stderr)
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    sys.exit(main())
