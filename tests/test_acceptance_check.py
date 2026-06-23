"""Tests for the M04 acceptance harness (scripts/research/run_acceptance_check.py).

Deterministic structural checks and the verbatim tripwire are tested for real.
The judge (LLM) and embedding signals are exercised only behind stubs — no test
makes a real network/LLM/embedding call by default (T-04.05).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cce.discovery.embeddings import EmbeddingResult, EmbeddingUnavailableError
from cce.llm.base import LLMResponse

# The harness lives in scripts/research/, outside the importable package. Add it
# to sys.path the same way the script reuses run_score_sweep.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "research"))
import run_acceptance_check as rac  # noqa: E402

_PAGES = _REPO_ROOT / "docs" / "internal" / "pages-converted"
_BAD_TOPIC = _REPO_ROOT / "output" / "mdx" / "loneliness-social-isolation-and-health"


# --------------------------------------------------------------------------- #
# Stubs (no network)
# --------------------------------------------------------------------------- #


class StubEmbedder:
    """Returns a fixed vector list regardless of input (duck-typed embedder)."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=self._vectors, model="stub", dimensions=len(self._vectors[0])
        )


class FailingEmbedder:
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise EmbeddingUnavailableError("embedding service down")


class RecordingLLM:
    """Captures complete() call args and returns canned content."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(
        self, messages, *, temperature=None, max_tokens=None, system=None
    ) -> LLMResponse:
        self.calls.append(
            {"messages": messages, "temperature": temperature, "system": system}
        )
        return LLMResponse(content=self.content)


# --------------------------------------------------------------------------- #
# (1) deterministic structural checks — both branches each (T-04.01, T-04.05)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_has_scaffolding_heading_hit() -> None:
    body = "# Loneliness and Health\n\n## Overview\n\nSome prose.\n\n## Closing Frame\n"
    hits = rac.has_scaffolding_heading(body)
    assert "## Overview" in hits
    assert "## Closing Frame" in hits


@pytest.mark.unit
def test_has_scaffolding_heading_clean() -> None:
    body = "# Loneliness and Health\n\n## Definition and mechanisms\n\nText.\n"
    assert rac.has_scaffolding_heading(body) == []


@pytest.mark.unit
def test_duplicate_url_citations_hit() -> None:
    citations = [
        {"url": "https://a.example", "index": 1},
        {"url": "https://a.example", "index": 2},
        {"url": "https://b.example", "index": 3},
    ]
    assert rac.duplicate_url_citations(citations) == {"https://a.example": 2}


@pytest.mark.unit
def test_duplicate_url_citations_clean() -> None:
    citations = [
        {"url": "https://a.example", "index": 1},
        {"url": "https://b.example", "index": 2},
    ]
    assert rac.duplicate_url_citations(citations) == {}


@pytest.mark.unit
def test_dimensions_in_body_hit() -> None:
    body = "Framing Through Eight Dimensions of Well-Being\n\nPhysical health is one domain."
    assert rac._dimensions_in_body(body) is True


@pytest.mark.unit
def test_dimensions_in_body_clean() -> None:
    body = "Loneliness is the subjective feeling of lacking desired connection."
    assert rac._dimensions_in_body(body) is False


# --------------------------------------------------------------------------- #
# verbatim tripwire — both branches (T-04.03, T-04.05)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_verbatim_copy_tripwire_hit() -> None:
    shared = (
        "Loneliness and social isolation are associated with a twenty nine percent "
        "increased risk of heart disease and a thirty two percent increased risk of "
        "stroke according to a large meta analysis of longitudinal cohort studies."
    )
    drafts = {
        "learn": shared + " The learn article adds a definition here.",
        "apply": shared + " The apply article adds an action here.",
    }
    hits = rac.verbatim_copy_tripwire(drafts)
    assert len(hits) == 1
    assert hits[0][2] >= 0.5


@pytest.mark.unit
def test_verbatim_copy_tripwire_clean() -> None:
    drafts = {
        "learn": "Loneliness is the subjective experience of social disconnection.",
        "apply": "Regular physical movement supports cardiovascular health and mood.",
    }
    assert rac.verbatim_copy_tripwire(drafts) == []


# --------------------------------------------------------------------------- #
# embedding signal — stubbed, including the EmbeddingUnavailableError path
# (T-04.03, T-04.05)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_embedding_near_duplicate_flags_identical_vectors() -> None:
    claims = {
        "learn": ["WHO estimates one in six are lonely."],
        "explore": ["The WHO has estimated one in six people are lonely."],
    }
    embedder = StubEmbedder([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    pairs = await rac.embedding_near_duplicate_claims(claims, embedder)
    assert len(pairs) == 1
    path_a, path_b, _summary, score = pairs[0]
    assert {path_a, path_b} == {"learn", "explore"}
    assert score >= 0.85


@pytest.mark.unit
async def test_embedding_near_duplicate_ignores_orthogonal_vectors() -> None:
    claims = {
        "learn": ["Loneliness harms the heart."],
        "explore": ["Curated resources for staying connected."],
    }
    embedder = StubEmbedder([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    assert await rac.embedding_near_duplicate_claims(claims, embedder) == []


@pytest.mark.unit
async def test_embedding_near_duplicate_swallows_unavailable() -> None:
    claims = {"learn": ["a claim"], "explore": ["another claim"]}
    assert await rac.embedding_near_duplicate_claims(claims, FailingEmbedder()) == []


# --------------------------------------------------------------------------- #
# judge — guard + determinism contract, stubbed (T-04.02, T-04.05)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_judge_repetition_skipped_without_llm() -> None:
    result = await rac.judge_repetition({"learn": "x", "explore": "y"}, None)
    assert result["verdict"] == "skipped"
    assert result["offending_passages"] == []


@pytest.mark.unit
async def test_judge_repetition_uses_temperature_zero_and_fixed_rubric() -> None:
    llm = RecordingLLM(
        '{"verdict": "fail", "offending_passages": ["restated WHO stat"], '
        '"rationale": "EXPLORE re-explains LEARN."}'
    )
    result = await rac.judge_repetition({"learn": "L", "explore": "E"}, llm)
    assert result["verdict"] == "fail"
    assert result["offending_passages"] == ["restated WHO stat"]
    assert len(llm.calls) == 1
    assert llm.calls[0]["temperature"] == 0.0
    assert llm.calls[0]["system"] == rac._JUDGE_RUBRIC


# --------------------------------------------------------------------------- #
# real-exemplar validations (filesystem only, no network) — T-04.01, T-04.03
# --------------------------------------------------------------------------- #


def _wrap_mdx(body: str) -> str:
    """Wrap flat markdown in the minimal metadata envelope extract_body expects."""
    return 'export const metadata = {\n  "title": "x"\n}\n\n' + body


@pytest.mark.integration
def test_dimensions_placement_on_client_exemplar(tmp_path: Path) -> None:
    """On the client's corrected loneliness trio: dimensions in EXPLORE, not LEARN."""
    topic = tmp_path / "loneliness"
    for role in ("learn", "explore"):
        d = topic / role
        d.mkdir(parents=True)
        src = (_PAGES / f"loneliness-{role}.md").read_text()
        (d / "page.mdx").write_text(_wrap_mdx(src))
    result = rac.dimensions_placement(topic)
    assert result["explore_has_dimensions"] is True
    assert result["learn_has_dimensions"] is False
    assert result["ok"] is True


@pytest.mark.integration
def test_dimensions_placement_on_bad_engine_output() -> None:
    """The current engine output puts dimensions in LEARN — the defect (ok=False)."""
    if not _BAD_TOPIC.exists():
        pytest.skip("output/mdx loneliness trio not present")
    result = rac.dimensions_placement(_BAD_TOPIC)
    assert result["learn_has_dimensions"] is True
    assert result["ok"] is False


@pytest.mark.integration
def test_scaffolding_flagged_on_bad_engine_output() -> None:
    """Structural layer flags >= 1 scaffolding heading on the bad MDX trio."""
    if not _BAD_TOPIC.exists():
        pytest.skip("output/mdx loneliness trio not present")
    learn_body = rac.extract_body(_BAD_TOPIC / "learn" / "page.mdx")
    assert learn_body is not None
    assert rac.has_scaffolding_heading(learn_body)


@pytest.mark.integration
def test_verbatim_tripwire_quiet_on_client_trio() -> None:
    """The client trio sits below the 0.5 coef bar (ADR-007) — no tripwire hits."""
    if not _PAGES.exists():
        pytest.skip("pages-converted references not present")
    drafts = rac.load_text_trio(_PAGES)
    assert len(drafts) == 3
    assert rac.verbatim_copy_tripwire(drafts) == []
