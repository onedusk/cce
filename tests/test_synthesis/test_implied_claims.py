"""Tests for the implied-claim checker (humanization M04)."""

from __future__ import annotations

import json

import pytest

from cce.config.markers import HumanizationMarkers, load_markers
from cce.config.types import ImpliedClaimsConfig
from cce.llm.base import LLMResponse
from cce.models.evidence import Evidence
from cce.synthesis.implied_claims import (
    ContrastiveFrame,
    ImpliedClaimChecker,
)
from tests.conftest import MockLLMProvider, make_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def markers() -> HumanizationMarkers:
    return load_markers("config/humanization_markers.yaml")


class StubStore:
    """Minimal EvidenceStore stub returning a scripted result for any topic search."""

    def __init__(self, results: list[Evidence] | None = None) -> None:
        self._results = results or []
        self.search_calls: list[dict] = []

    async def search(
        self, *, url: str | None = None, topic: str | None = None, limit: int = 50
    ) -> list[Evidence]:
        self.search_calls.append({"topic": topic, "limit": limit})
        return self._results[:limit]

    # Other protocol methods aren't called by the checker — no need to stub.


def _topic_extract_response(topic: str, rationale: str = "extracted") -> str:
    return json.dumps({"dismissed_topic": topic, "rationale": rationale})


def _make_checker(
    *,
    markers: HumanizationMarkers,
    counter_evidence: list[Evidence] | None = None,
    config: ImpliedClaimsConfig | None = None,
    extracted_topics: list[str] | None = None,
) -> tuple[ImpliedClaimChecker, MockLLMProvider, StubStore]:
    topics = extracted_topics or ["sleeping pills"]
    llm = MockLLMProvider(
        [LLMResponse(content=_topic_extract_response(t), model="mock") for t in topics]
    )
    store = StubStore(results=counter_evidence or [])
    checker = ImpliedClaimChecker(
        llm=llm,
        evidence_store=store,
        config=config or ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    return checker, llm, store


# --- Frame detection (no LLM, no store) ---


def test_detect_frames_finds_unlike_pattern(markers):
    checker = ImpliedClaimChecker(
        llm=MockLLMProvider(),
        evidence_store=StubStore(),
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    frames = checker._detect_frames("Unlike sleeping pills, CBT-I works.")

    assert frames
    assert any("Unlike" in f.matched_text for f in frames)


def test_detect_frames_returns_empty_when_no_contrast(markers):
    checker = ImpliedClaimChecker(
        llm=MockLLMProvider(),
        evidence_store=StubStore(),
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    frames = checker._detect_frames("CBT-I targets the underlying habits.")

    assert frames == []


# --- Full check() flow ---


async def test_check_emits_annotation_when_counter_exists(markers):
    counter = [make_evidence() for _ in range(5)]
    cited = [make_evidence() for _ in range(10)]  # ratio 0.5 > 0.15
    checker, _llm, _store = _make_checker(markers=markers, counter_evidence=counter)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I addresses the root cause.",
        cited_evidence=cited,
    )

    assert len(annotations) == 1
    assert annotations[0].dismissed_topic == "sleeping pills"
    assert annotations[0].counter_evidence_ids == [ev.id for ev in counter[:5]]
    assert annotations[0].has_counter_evidence is True
    assert "spectrum" in annotations[0].rewrite_hint
    assert "sleeping pills" in annotations[0].rewrite_hint


async def test_check_skips_frame_with_no_counter_evidence(markers):
    """Empty counter-evidence search → no annotation emitted."""
    cited = [make_evidence() for _ in range(10)]
    checker, _llm, _store = _make_checker(markers=markers, counter_evidence=[])

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    assert annotations == []


async def test_release_valve_suppresses_low_ratio_counter(markers):
    """1 counter / 10 cited = 0.1 ≤ 0.15 default release valve → suppressed."""
    counter = [make_evidence()]
    cited = [make_evidence() for _ in range(10)]
    checker, _llm, _store = _make_checker(markers=markers, counter_evidence=counter)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    assert annotations == []


async def test_release_valve_does_not_suppress_when_cited_empty(markers):
    """Empty cited pool → no denominator; do NOT auto-suppress (v1 design)."""
    counter = [make_evidence()]
    checker, _llm, _store = _make_checker(markers=markers, counter_evidence=counter)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=[],
    )

    assert len(annotations) == 1


async def test_search_strategy_embedding_raises(markers):
    """The 'embedding' strategy is reserved for a post-H4 upgrade."""
    counter = [make_evidence() for _ in range(5)]
    checker, _llm, _store = _make_checker(
        markers=markers,
        counter_evidence=counter,
        config=ImpliedClaimsConfig(enabled=True, search_strategy="embedding"),
    )

    with pytest.raises(NotImplementedError):
        await checker.check(
            "Unlike sleeping pills, CBT-I works.", cited_evidence=[make_evidence()]
        )


async def test_dismissed_topic_extraction_uses_zero_temperature(markers):
    counter = [make_evidence() for _ in range(5)]
    cited = [make_evidence() for _ in range(10)]
    checker, llm, _store = _make_checker(markers=markers, counter_evidence=counter)

    await checker.check("Unlike sleeping pills, CBT-I works.", cited_evidence=cited)

    # The first (and only) LLM call is the topic extraction
    assert llm.calls
    assert llm.calls[0]["temperature"] == 0.0


async def test_annotation_rewrite_hint_includes_first_five_evidence_ids(markers):
    counter = [make_evidence() for _ in range(8)]
    cited = [make_evidence() for _ in range(10)]
    checker, _llm, _store = _make_checker(markers=markers, counter_evidence=counter)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.", cited_evidence=cited
    )

    # Hint truncates evidence id list to 5 (Stage 2 implementation)
    hint = annotations[0].rewrite_hint
    assert all(ev.id in hint for ev in counter[:5])
    # The 6th id should not be present
    assert counter[5].id not in hint


async def test_check_returns_empty_when_no_frames_detected(markers):
    """No contrastive frames → no LLM calls, no store calls, empty annotations."""
    checker, llm, store = _make_checker(
        markers=markers, counter_evidence=[make_evidence()]
    )

    annotations = await checker.check(
        "CBT-I targets the underlying habits keeping people awake.",
        cited_evidence=[make_evidence()],
    )

    assert annotations == []
    assert llm.calls == []
    assert store.search_calls == []


def test_contrastive_frame_is_frozen():
    """ContrastiveFrame is a frozen dataclass — mutating raises FrozenInstanceError."""
    import dataclasses

    frame = ContrastiveFrame(
        matched_text="Unlike X,", char_start=0, char_end=9, pattern_index=0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.matched_text = "changed"  # type: ignore[misc]


# --- Subtype handling (0.2.0 — Phase B Layer 2) ---


def test_detect_frames_tags_parasitic_vs_genuine(markers):
    """Parasitic and genuine-alternative regexes tag frames with the
    corresponding ``kind`` field."""
    checker = ImpliedClaimChecker(
        llm=MockLLMProvider(),
        evidence_store=StubStore(),
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    body = (
        "Unlike sleeping pills, CBT-I addresses root causes. "
        "This is not a quick fix. It is a durable intervention."
    )
    frames = checker._detect_frames(body)

    kinds = {f.kind for f in frames}
    assert "genuine_alternative" in kinds
    assert "parasitic" in kinds


async def test_check_skips_parasitic_frames_no_llm_call(markers):
    """Parasitic frames bypass LLM topic extraction entirely — saves one
    request per frame and avoids the fragment-too-short warnings the
    extractor logs on them."""
    cited = [make_evidence() for _ in range(10)]
    llm = MockLLMProvider([])  # no scripted responses — any call would raise
    store = StubStore(results=[])
    checker = ImpliedClaimChecker(
        llm=llm,
        evidence_store=store,
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )

    annotations = await checker.check(
        "Boredom is not a problem to be solved. It is a signal to be heard.",
        cited_evidence=cited,
    )

    assert annotations == []
    assert llm.calls == [], "parasitic frames must not trigger LLM topic extraction"
    assert store.search_calls == [], "parasitic frames must not hit the store"


async def test_check_still_processes_genuine_alternative_when_parasitic_present(markers):
    """Mixed body: parasitic frames are skipped; genuine_alternative
    frames still go through the normal topic-extract → counter-search pipeline."""
    cited = [make_evidence() for _ in range(10)]
    counter = [make_evidence() for _ in range(3)]  # ratio 0.3 > 0.15
    checker, llm, store = _make_checker(
        markers=markers,
        counter_evidence=counter,
        extracted_topics=["sleeping pills"],
    )

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works. "
        "This is not a shortcut. It is a longer investment.",
        cited_evidence=cited,
    )

    assert len(annotations) == 1  # only the genuine_alternative frame triggered
    assert annotations[0].frame.kind == "genuine_alternative"
    # LLM was called exactly once — for the genuine-alternative frame only.
    assert len(llm.calls) == 1
