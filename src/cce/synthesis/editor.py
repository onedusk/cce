"""Editor agent (humanization M03).

Takes a writer's cited draft and rewrites it for naturalness, preserving every
``[ev:ID]`` marker attached to its original claim. Hard constraints (enforced
in the prompt AND checked in :func:`_extract_citation_ids`):

1. Every citation marker in the input appears in the output.
2. No new factual claims — the Editor is not a Writer.
3. Output stays within the caller's word-count tolerance.

The verifier remains the single trust gate (ADR-005). If the Editor drops or
adds a citation, ``EditorOutput.citations_preserved`` is ``False`` and the
pipeline retains the writer's original draft so the verifier still runs
against known-good content.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from cce.config.types import EditorConfig
from cce.llm.base import LLMMessage, LLMProvider
from cce.llm.retry import with_llm_retry
from cce.models.content import ContentUnit
from cce.models.paths import PathConfig
from cce.models.style import StyleScores
from cce.parsing import extract_json

logger = logging.getLogger(__name__)


# Canonical citation format is [ev:ID] (colon). The writer prompt, the
# verifier prompt, and the gate all emit/accept this form. Strict matching
# here means a malformed [ev_ID] variant in the writer's output won't be
# silently treated as "preserved" by the citation drift check.
_CITATION_RE = re.compile(r"\[ev:[^\]]+\]")
_WORD_RE = re.compile(r"\b[\w'-]+\b")


EDITOR_SYSTEM_PROMPT = """\
You are a style editor. You receive a draft that is already factually accurate \
and properly cited. Your job is to rewrite for natural prose — varied sentence \
length, organic transitions, declarative tone where the evidence is strong, \
acknowledgment of nuance instead of binary framing.

HARD CONSTRAINTS — your output is rejected if any are violated:

1. Every [ev:EVIDENCE_ID] citation marker in the input MUST appear in your \
output, attached to the same factual claim it cites in the input.
2. You MUST NOT introduce any factual claim that was not in the input. You \
are an editor, not a writer.
3. You MUST NOT remove or relocate citations to different claims.
4. Stay within the target word count tolerance specified by the caller.

WHAT TO CHANGE:
- Replace formulaic vocabulary (delve, underscore, showcasing, crucial, \
robust, comprehensive, multifaceted, pivotal, etc.) with natural alternatives.
- Replace hyperbolic descriptors (groundbreaking, vital, invaluable, profound) \
with calibrated language that matches the actual significance of the claim.
- Reduce em dash (—) usage. AI prose overuses em dashes by roughly 5-10x \
compared to typical human writing. Replace most em dashes with commas, \
periods, parentheses, or full sentence breaks. Reserve em dashes for genuine \
parenthetical asides where no other punctuation works as well.
- Vary sentence length — mix short fragments with longer constructions.
- Restructure paragraphs that follow a rigid topic-sentence / evidence / \
summary template.
- Replace formulaic transitions ("Furthermore,", "Additionally,", "Moreover,", \
"In conclusion,") with organic connective tissue.
- Reduce hedging and AI stock phrases ("it should be noted", "a testament to", \
"in today's fast-paced world", "at the intersection of"). Convert "research \
suggests X may be effective" to "X works" where the evidence is strong.
- For contrastive frames ("Unlike X, Y does Z"), apply the spectrum principle: \
acknowledge where the dismissed side is valid in context.

OUTPUT FORMAT:
Return a JSON object with exactly these fields:
{
  "edited_content": "<rewritten markdown with all [ev:ID] markers preserved>",
  "notes": "<brief summary of what you changed and why>"
}\
"""


@dataclass
class EditorOutput:
    """Result of an Editor invocation."""

    edited_content: str
    notes: str
    citations_preserved: bool
    word_count_before: int
    word_count_after: int
    raw_response: str
    token_usage: dict[str, int]

    @property
    def succeeded(self) -> bool:
        """True iff citations are intact AND content is non-empty."""
        return self.citations_preserved and bool(self.edited_content)


class Editor:
    """LLM-based stylistic rewriter with citation-preservation check."""

    def __init__(self, llm: LLMProvider, config: EditorConfig) -> None:
        self._llm = llm
        self._config = config

    async def edit(
        self,
        unit: ContentUnit,
        path_config: PathConfig | None = None,
        scores: StyleScores | None = None,
        annotations: list[str] | None = None,
    ) -> EditorOutput:
        """Rewrite ``unit.content`` for naturalness, preserving citations.

        Args:
            unit: The cited draft from the writer.
            path_config: Optional path-specific tone/word-budget constraints.
            scores: The StyleScores that triggered this invocation, used to
                focus the rewrite on metrics that actually failed.
            annotations: Rewrite hints from the ImpliedClaimChecker (H4/M04).
                Each string points at a contrastive frame with counter-evidence;
                the Editor uses them to apply the spectrum principle with
                specific evidence references.
        """
        original_citations = _extract_citation_ids(unit.content)
        word_count_before = _word_count(unit.content)

        user_prompt = self._build_user_prompt(
            unit=unit,
            path_config=path_config,
            scores=scores,
            annotations=annotations or [],
        )

        async def _attempt() -> EditorOutput:
            response = await self._llm.complete(
                [LLMMessage(role="user", content=user_prompt)],
                system=EDITOR_SYSTEM_PROMPT,
                temperature=self._config.temperature,
            )
            return self._parse_response(
                raw=response.content,
                original_citations=original_citations,
                word_count_before=word_count_before,
                token_usage=response.usage or {},
            )

        return await with_llm_retry(_attempt)

    def _build_user_prompt(
        self,
        *,
        unit: ContentUnit,
        path_config: PathConfig | None,
        scores: StyleScores | None,
        annotations: list[str],
    ) -> str:
        """Compose the per-call user prompt."""
        parts: list[str] = [f"Path: {unit.path}"]

        if path_config is not None:
            parts.append(f"Tone: {path_config.tone}")
            parts.append(f"Structure: {path_config.structure}")
            if path_config.max_words:
                drift = self._config.max_words_drift_pct
                low = int(path_config.max_words * (1 - drift))
                high = int(path_config.max_words * (1 + drift))
                parts.append(f"Target word count: {low}-{high}")

        if scores is not None:
            # Flag only the metrics that actually failed so the editor
            # focuses its attention.
            flagged: list[str] = []
            if scores.suppressed_vocab_hits > 0:
                flagged.append(
                    f"suppressed vocabulary: {scores.suppressed_vocab_hits} hits"
                )
            if scores.formulaic_transition_count > 0:
                flagged.append(
                    f"formulaic transitions: {scores.formulaic_transition_count}"
                )
            if scores.contrastive_frame_count > 0:
                flagged.append(f"contrastive frames: {scores.contrastive_frame_count}")
            if scores.hedging_phrase_count > 0:
                flagged.append(f"hedging phrases: {scores.hedging_phrase_count}")
            if scores.em_dash_count > 0:
                flagged.append(f"em dashes: {scores.em_dash_count}")
            if flagged:
                parts.append("Scorer flags to focus on: " + "; ".join(flagged))

        if annotations:
            parts.append("Implied-claim annotations (H4):")
            parts.extend(f"- {hint}" for hint in annotations)

        parts.append("")
        parts.append("=== DRAFT START ===")
        parts.append(unit.content)
        parts.append("=== DRAFT END ===")
        parts.append("")
        parts.append(
            "Rewrite the draft per the hard constraints and style guidance in "
            "the system prompt. Return the JSON object described there."
        )
        return "\n".join(parts)

    def _parse_response(
        self,
        *,
        raw: str,
        original_citations: set[str],
        word_count_before: int,
        token_usage: dict[str, int],
    ) -> EditorOutput:
        """Parse JSON response and verify citation preservation."""
        parsed = extract_json(raw) or {}
        edited = str(parsed.get("edited_content", ""))
        notes = str(parsed.get("notes", ""))

        edited_citations = _extract_citation_ids(edited)
        preserved = edited_citations == original_citations
        if not preserved:
            missing = original_citations - edited_citations
            extra = edited_citations - original_citations
            logger.warning(
                "Editor citation drift: missing=%d, extra=%d "
                "(first missing=%s, first extra=%s)",
                len(missing),
                len(extra),
                next(iter(missing), None),
                next(iter(extra), None),
            )

        return EditorOutput(
            edited_content=edited,
            notes=notes,
            citations_preserved=preserved,
            word_count_before=word_count_before,
            word_count_after=_word_count(edited),
            raw_response=raw,
            token_usage=token_usage,
        )


def _extract_citation_ids(content: str) -> set[str]:
    """Extract the set of [ev:ID] markers referenced in a body."""
    return set(_CITATION_RE.findall(content))


def _word_count(content: str) -> int:
    """Body word count with citation markers stripped."""
    return len(_WORD_RE.findall(_CITATION_RE.sub("", content)))
