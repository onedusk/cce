"""Implied-claim checker (humanization M04).

Detects contrastive frames in a draft, extracts the dismissed-side topic via
the LLM, and searches the evidence store for material supporting that side.
Surfaces flagged frames as annotations for the Editor (M03).

Why this matters (PDR-002): the verifier checks every *explicit* claim, but
contrastive constructions like "Unlike sleeping pills, CBT-I addresses root
causes" smuggle uncited *implied* claims about the dismissed side. Extending
the trust contract to those implied claims is an accuracy fix, not a style
fix — the spectrum-principle rewrite is a side benefit.

The release valve (``dismissal_release_valve_ratio``) lets binary framing
stand when counter-evidence is small relative to the cited pool: if you have
ten sources backing CBT-I and one mentioning that sleeping pills can help in
acute insomnia, the contrast is editorially fair with a brief qualifier
rather than a full spectrum rewrite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cce.config.markers import HumanizationMarkers
from cce.config.types import ImpliedClaimsConfig
from cce.evidence.store import EvidenceStore
from cce.llm.base import LLMMessage, LLMProvider
from cce.llm.retry import with_llm_retry
from cce.models.evidence import Evidence
from cce.parsing import extract_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContrastiveFrame:
    """A detected contrastive construction in a draft body."""

    matched_text: str
    char_start: int
    char_end: int
    pattern_index: int


@dataclass
class ImpliedClaimAnnotation:
    """An annotation surfaced to the Editor for one flagged contrastive frame.

    ``rewrite_hint`` is the human-readable instruction the Editor will see
    in its prompt; ``counter_evidence_ids`` lets downstream tooling cross-
    reference back to the evidence store for audit.
    """

    frame: ContrastiveFrame
    dismissed_topic: str
    counter_evidence_ids: list[str] = field(default_factory=list)
    rewrite_hint: str = ""

    @property
    def has_counter_evidence(self) -> bool:
        return bool(self.counter_evidence_ids)


_DISMISSED_TOPIC_PROMPT = """\
You are extracting the dismissed side of a contrastive statement. Given a \
short text fragment containing a contrast (e.g. "Unlike sleeping pills, \
CBT-I works"), identify the topic being dismissed and return a one-line \
topic phrase suitable for an evidence-store keyword search.

Return JSON: {"dismissed_topic": "<topic>", "rationale": "<why>"}\
"""


class ImpliedClaimChecker:
    """Find contrastive frames whose dismissed side has supporting evidence."""

    def __init__(
        self,
        llm: LLMProvider,
        evidence_store: EvidenceStore,
        config: ImpliedClaimsConfig,
        markers: HumanizationMarkers,
    ) -> None:
        self._llm = llm
        self._store = evidence_store
        self._config = config
        self._patterns = markers.compiled_contrastive_patterns()

    async def check(
        self,
        content: str,
        cited_evidence: list[Evidence],
    ) -> list[ImpliedClaimAnnotation]:
        """Return annotations for frames whose dismissed side has counter-evidence.

        Args:
            content: Draft body (citation markers may be present).
            cited_evidence: Evidence the writer drew from. Used to compute the
                release-valve ratio: if the counter-evidence pool is small
                relative to the cited pool on the dismissed topic, the
                contrast is editorially permissible without a spectrum rewrite.
        """
        frames = self._detect_frames(content)
        if not frames:
            return []

        annotations: list[ImpliedClaimAnnotation] = []
        for frame in frames:
            dismissed = await self._extract_dismissed_topic(frame)
            if not dismissed:
                continue
            counter = await self._search_counter_evidence(dismissed)
            if not counter:
                continue
            if self._below_release_valve(counter, cited_evidence):
                logger.info(
                    "Release valve suppressed implied-claim annotation for "
                    "topic=%r (counter=%d, cited=%d)",
                    dismissed,
                    len(counter),
                    len(cited_evidence),
                )
                continue
            annotations.append(
                ImpliedClaimAnnotation(
                    frame=frame,
                    dismissed_topic=dismissed,
                    counter_evidence_ids=[ev.id for ev in counter],
                    rewrite_hint=self._build_hint(dismissed, counter),
                )
            )
        return annotations

    def _detect_frames(self, content: str) -> list[ContrastiveFrame]:
        """Run every compiled contrastive regex over the body."""
        results: list[ContrastiveFrame] = []
        for idx, pattern in enumerate(self._patterns):
            for m in pattern.finditer(content):
                results.append(
                    ContrastiveFrame(
                        matched_text=m.group(0),
                        char_start=m.start(),
                        char_end=m.end(),
                        pattern_index=idx,
                    )
                )
        return sorted(results, key=lambda f: f.char_start)

    async def _extract_dismissed_topic(self, frame: ContrastiveFrame) -> str:
        """Ask the LLM to name the dismissed-side topic in one line."""

        async def _attempt() -> str:
            response = await self._llm.complete(
                [LLMMessage(role="user", content=f"Fragment: {frame.matched_text}")],
                system=_DISMISSED_TOPIC_PROMPT,
                temperature=0.0,
            )
            parsed = extract_json(response.content) or {}
            return str(parsed.get("dismissed_topic", "")).strip()

        return await with_llm_retry(_attempt)

    async def _search_counter_evidence(self, dismissed_topic: str) -> list[Evidence]:
        """Search the evidence store for material supporting the dismissed topic."""
        if self._config.search_strategy in ("keyword", "llm_extract"):
            # v1: both strategies hit the same keyword search — the LLM
            # extracted the topic phrase, the store does keyword lookup.
            return await self._store.search(
                topic=dismissed_topic,
                limit=self._config.counter_evidence_search_limit,
            )
        # search_strategy == "embedding" — deferred (Stage 1 ImpliedClaimsConfig)
        raise NotImplementedError("Embedding-based counter-search deferred to post-H4")

    def _below_release_valve(
        self,
        counter: list[Evidence],
        cited_evidence: list[Evidence],
    ) -> bool:
        """Return True when counter-evidence is small enough to permit the contrast.

        Empty cited evidence returns False — without a denominator we can't
        compute a meaningful ratio, so we don't auto-suppress.
        """
        if not cited_evidence:
            return False
        ratio = len(counter) / len(cited_evidence)
        return ratio <= self._config.dismissal_release_valve_ratio

    def _build_hint(self, dismissed: str, counter: list[Evidence]) -> str:
        ids = ", ".join(ev.id for ev in counter[:5])
        return (
            f"This contrast dismisses '{dismissed}', but evidence [{ids}] "
            f"shows '{dismissed}' is valid in context. Rewrite to acknowledge "
            f"the spectrum rather than collapse to a binary."
        )
