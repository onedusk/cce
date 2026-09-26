"""Tests for the implied-claim checker (humanization M04)."""

from __future__ import annotations

import json

import pytest

from cce.config.markers import HumanizationMarkers, load_markers
from cce.config.types import HumanizationThresholds, ImpliedClaimsConfig
from cce.llm.base import IncompleteResponseError, LLMResponse
from cce.models.evidence import Evidence
from cce.synthesis.implied_claims import (
    ContrastiveFrame,
    ImpliedClaimChecker,
)
from cce.synthesis.scoring import Scorer
from tests.conftest import MockLLMProvider, make_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def markers() -> HumanizationMarkers:
    return load_markers("config/humanization_markers.yaml")


def _counter(n: int) -> list[Evidence]:
    """Path evidence supporting the dismissed side ("sleeping pills")."""
    return [
        make_evidence(excerpt="Sleeping pills help short-term insomnia in trials.")
        for _ in range(n)
    ]


def _unrelated(n: int) -> list[Evidence]:
    """Path evidence that never mentions the dismissed side."""
    return [make_evidence() for _ in range(n)]


def _topic_extract_response(topic: str, rationale: str = "extracted") -> str:
    return json.dumps({"dismissed_topic": topic, "rationale": rationale})


def _make_checker(
    *,
    markers: HumanizationMarkers,
    config: ImpliedClaimsConfig | None = None,
    extracted_topics: list[str] | None = None,
) -> tuple[ImpliedClaimChecker, MockLLMProvider]:
    topics = extracted_topics or ["sleeping pills"]
    llm = MockLLMProvider(
        [LLMResponse(content=_topic_extract_response(t), model="mock") for t in topics]
    )
    checker = ImpliedClaimChecker(
        llm=llm,
        config=config or ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    return checker, llm


# --- Frame detection (no LLM, no store) ---


def test_detect_frames_finds_unlike_pattern(markers):
    checker = ImpliedClaimChecker(
        llm=MockLLMProvider(),
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    frames = checker._detect_frames("Unlike sleeping pills, CBT-I works.")

    assert frames
    assert any("Unlike" in f.matched_text for f in frames)


def test_detect_frames_returns_empty_when_no_contrast(markers):
    checker = ImpliedClaimChecker(
        llm=MockLLMProvider(),
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    frames = checker._detect_frames("CBT-I targets the underlying habits.")

    assert frames == []


# --- Full check() flow ---


async def test_check_emits_annotation_when_counter_exists(markers):
    counter = _counter(5)
    cited = counter + _unrelated(5)  # ratio 0.5 > 0.15
    checker, _llm = _make_checker(markers=markers)

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
    cited = _unrelated(10)
    checker, _llm = _make_checker(markers=markers)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    assert annotations == []


async def test_release_valve_suppresses_low_ratio_counter(markers):
    """1 counter / 10 cited = 0.1 ≤ 0.15 default release valve → suppressed."""
    cited = _counter(1) + _unrelated(9)
    checker, _llm = _make_checker(markers=markers)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    assert annotations == []


async def test_check_emits_nothing_when_path_pool_empty(markers):
    """Counter-evidence comes from the path pool, so an empty pool has none.
    The release valve itself still never auto-suppresses without a
    denominator (v1 design)."""
    checker, _llm = _make_checker(markers=markers)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=[],
    )

    assert annotations == []
    assert checker._below_release_valve(_counter(1), []) is False


async def test_search_strategy_embedding_raises(markers):
    """The 'embedding' strategy is reserved for a post-H4 upgrade."""
    checker, _llm = _make_checker(
        markers=markers,
        config=ImpliedClaimsConfig(enabled=True, search_strategy="embedding"),
    )

    with pytest.raises(NotImplementedError):
        await checker.check(
            "Unlike sleeping pills, CBT-I works.",
            cited_evidence=[make_evidence()],
        )


async def test_dismissed_topic_extraction_uses_zero_temperature(markers):
    cited = _counter(5) + _unrelated(5)
    checker, llm = _make_checker(markers=markers)

    await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    # The first (and only) LLM call is the topic extraction
    assert llm.calls
    assert llm.calls[0]["temperature"] == 0.0


async def test_annotation_rewrite_hint_includes_first_five_evidence_ids(markers):
    counter = _counter(8)
    cited = counter + _unrelated(2)
    checker, _llm = _make_checker(markers=markers)

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.",
        cited_evidence=cited,
    )

    # Hint truncates evidence id list to 5 (Stage 2 implementation)
    hint = annotations[0].rewrite_hint
    assert all(ev.id in hint for ev in counter[:5])
    # The 6th id should not be present
    assert counter[5].id not in hint


async def test_check_returns_empty_when_no_frames_detected(markers):
    """No contrastive frames → no LLM calls, empty annotations."""
    checker, llm = _make_checker(markers=markers)

    annotations = await checker.check(
        "CBT-I targets the underlying habits keeping people awake.",
        cited_evidence=[make_evidence()],
    )

    assert annotations == []
    assert llm.calls == []


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
    checker = ImpliedClaimChecker(
        llm=llm,
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )

    annotations = await checker.check(
        "Boredom is not a problem to be solved. It is a signal to be heard.",
        cited_evidence=cited,
    )

    assert annotations == []
    assert llm.calls == [], "parasitic frames must not trigger LLM topic extraction"


async def test_check_still_processes_genuine_alternative_when_parasitic_present(
    markers,
):
    """Mixed body: parasitic frames are skipped; genuine_alternative
    frames still go through the normal topic-extract → counter-search pipeline."""
    cited = _counter(3) + _unrelated(7)  # ratio 0.3 > 0.15
    checker, llm = _make_checker(
        markers=markers,
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


async def test_topic_extraction_raises_on_truncated_reply(markers):
    """B2: a truncated topic-extraction reply raises instead of being parsed."""
    llm = MockLLMProvider(
        [
            LLMResponse(
                content='{"dismissed_topic": "sleep',
                model="claude-sonnet-5",
                usage={"output_tokens": 16384},
                stop_reason="max_tokens",
            )
        ]
    )
    checker = ImpliedClaimChecker(
        llm=llm,
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )

    with pytest.raises(IncompleteResponseError, match="implied-claim checker"):
        await checker.check(
            "Unlike sleeping pills, CBT-I works.",
            cited_evidence=[make_evidence()],
        )

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        "Choose a routine rather than a pill.",
        "By contrast, CBT-I changes habits.",
    ],
)
async def test_check_skips_keyword_only_frames_no_llm_call(markers, body):
    """'rather than ' and 'by contrast' match only frame keywords, so the
    fragment names no dismissed topic: no extraction call, and the scorer
    still counts the frame."""
    llm = MockLLMProvider([])  # no scripted responses: any call would raise
    checker = ImpliedClaimChecker(
        llm=llm,
        config=ImpliedClaimsConfig(enabled=True),
        markers=markers,
    )
    assert checker._detect_frames(body), "fixture must contain a detected frame"

    annotations = await checker.check(body, cited_evidence=[make_evidence()])

    assert annotations == []
    assert llm.calls == []
    scores = Scorer(thresholds=HumanizationThresholds(), markers=markers).score(body)
    assert scores.contrastive_frame_count == 1


async def test_counter_search_matches_path_title_or_excerpt_up_to_limit(markers):
    """Counter-evidence is the path's own evidence naming the topic in its
    title or excerpt (case-insensitive, like the store's topic search),
    capped at counter_evidence_search_limit."""
    by_title = make_evidence(title="SLEEPING PILLS and older adults")
    by_excerpt = _counter(2)
    cited = [by_title, *by_excerpt, *_unrelated(2)]
    checker, _llm = _make_checker(
        markers=markers,
        config=ImpliedClaimsConfig(enabled=True, counter_evidence_search_limit=2),
    )

    annotations = await checker.check(
        "Unlike sleeping pills, CBT-I works.", cited_evidence=cited
    )

    assert annotations[0].counter_evidence_ids == [by_title.id, by_excerpt[0].id]
