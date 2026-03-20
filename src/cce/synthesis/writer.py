"""Evidence-constrained writer agent.

The writer produces a draft ONLY from stored evidence objects. It receives
a list of Evidence and a target output path, and emits structured content
with inline citations keyed to evidence IDs.

This is the hardest unsolved problem in the pipeline. The key constraint:
the LLM must not fill gaps from its training data. Every factual claim
must trace to a provided evidence excerpt.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from cce.llm.base import LLMMessage, LLMProvider, LLMResponse
from cce.models.content import (
    Citation,
    ClaimMapping,
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence
from cce.models.request import CurationRequest
from cce.parsing import extract_json

logger = logging.getLogger(__name__)

WRITER_SYSTEM_PROMPT = """\
You are an evidence-constrained content writer. You produce well-structured, \
accurate content using ONLY the evidence excerpts provided to you.

CRITICAL RULES:
1. Every factual claim you make MUST be supported by at least one evidence excerpt.
2. You MUST cite evidence using the format [ev:EVIDENCE_ID] inline after the claim.
3. You MUST NOT introduce any facts, statistics, dates, names, or claims that are \
not directly stated in the provided evidence excerpts.
4. If the evidence is insufficient to cover a subtopic, write: \
"[INSUFFICIENT EVIDENCE: <description of what's missing>]" instead of fabricating content.
5. If evidence sources conflict, explicitly state the conflict and cite both sides.
6. Use direct quotes sparingly -- paraphrase evidence accurately and cite it.

OUTPUT FORMAT:
Return a JSON object with exactly these fields:
{
  "content": "<markdown string with [ev:ID] citations inline>",
  "citations_used": ["ev_id1", "ev_id2", ...],
  "evidence_map": [
    {"claim": "<claim text>", "evidence_ids": ["ev_id1"]},
    ...
  ],
  "gaps": ["<description of any insufficient evidence areas>"]
}

Write in clear, accessible prose appropriate for the target audience. \
Structure the content with markdown headings and paragraphs.\
"""


def _build_evidence_block(evidence: list[Evidence]) -> str:
    """Format evidence objects as a numbered reference block for the LLM."""
    lines: list[str] = []
    for ev in evidence:
        meta_parts = [f"URL: {ev.url}"]
        if ev.title:
            meta_parts.append(f"Title: {ev.title}")
        if ev.author:
            meta_parts.append(f"Author: {ev.author}")
        if ev.published_at:
            meta_parts.append(f"Published: {ev.published_at.strftime('%Y-%m-%d')}")
        if ev.source_quality and ev.source_quality.domain_reputation:
            meta_parts.append(f"Reputation: {ev.source_quality.domain_reputation}")

        lines.append(f"--- EVIDENCE [{ev.id}] ---")
        lines.append(" | ".join(meta_parts))
        lines.append(ev.excerpt)
        lines.append("")

    return "\n".join(lines)


class Writer:
    """Evidence-constrained content writer."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def write(
        self,
        request: CurationRequest,
        evidence: list[Evidence],
        path: str,
        *,
        feedback: Optional[str] = None,
        lineage: Optional[ContentLineage] = None,
    ) -> WriterOutput:
        """Produce a draft for one output path from the given evidence.

        Args:
            request: The original curation request.
            evidence: Evidence objects to synthesize from.
            path: Which output path to write for.
            feedback: Optional verifier feedback from a previous iteration
                      (gaps to fill, claims to fix).
            lineage: Provenance metadata to attach to the content unit.
        """
        if not evidence:
            logger.warning("Writer called with no evidence for path '%s'", path)
            return WriterOutput(
                unit=None,
                gaps=["No evidence provided"],
                raw_response="",
            )

        evidence_block = _build_evidence_block(evidence)

        user_prompt = f"""Topic: {request.topic}
Subtopics: {", ".join(request.subtopics) if request.subtopics else "None specified"}
Target audience: {request.audience}
Output path: {path}

You have {len(evidence)} evidence excerpts to work with.

=== EVIDENCE START ===
{evidence_block}
=== EVIDENCE END ===
"""

        if feedback:
            user_prompt += f"""
=== VERIFIER FEEDBACK (from previous iteration) ===
{feedback}
=== END FEEDBACK ===

Address the feedback above. Fix unsupported claims, fill gaps where evidence \
exists, and mark remaining gaps as [INSUFFICIENT EVIDENCE].
"""

        messages = [LLMMessage(role="user", content=user_prompt)]

        logger.info(
            "Writer: generating draft for path '%s' with %d evidence objects",
            path,
            len(evidence),
        )

        response = await self._llm.complete(
            messages,
            system=WRITER_SYSTEM_PROMPT,
            temperature=0.2,  # low temp for factual consistency
        )

        return self._parse_response(response, evidence, path, lineage)

    def _parse_response(
        self,
        response: LLMResponse,
        evidence: list[Evidence],
        path: str,
        lineage: Optional[ContentLineage],
    ) -> WriterOutput:
        """Parse the LLM response into a ContentUnit."""
        raw = response.content.strip()

        # Try to extract JSON from the response
        parsed = extract_json(raw)

        if parsed is None:
            logger.warning(
                "Writer response was not valid JSON, treating as raw markdown"
            )
            # Fallback: treat the whole response as content with no structured metadata
            return WriterOutput(
                unit=ContentUnit(
                    id=f"cu_{uuid.uuid4().hex[:12]}",
                    path=path,
                    content=raw,
                    citations=[],
                    evidence_map=[],
                    scores=ContentScores(
                        confidence=0.0, coverage=0.0, source_diversity=0.0
                    ),
                    lineage=lineage
                    or ContentLineage(policy_id="", run_id="", engine_version=""),
                ),
                gaps=[
                    "Writer response was not structured JSON -- verification required"
                ],
                raw_response=raw,
            )

        # Build evidence ID lookup for URL resolution
        ev_lookup = {ev.id: ev for ev in evidence}

        # Parse citations
        citations_used = parsed.get("citations_used", [])
        citations = [
            Citation(evidence_id=eid, url=ev_lookup[eid].url)
            for eid in citations_used
            if eid in ev_lookup
        ]

        # Parse evidence map
        evidence_map_raw = parsed.get("evidence_map", [])
        evidence_map = [
            ClaimMapping(
                claim=item.get("claim", ""),
                evidence_ids=[
                    eid for eid in item.get("evidence_ids", []) if eid in ev_lookup
                ],
            )
            for item in evidence_map_raw
            if item.get("claim")
        ]

        gaps = parsed.get("gaps", [])
        content_text = parsed.get("content", "")

        # Calculate basic source diversity
        unique_urls = set()
        for eid in citations_used:
            if eid in ev_lookup:
                unique_urls.add(ev_lookup[eid].url)
        unique_available = {ev.url for ev in evidence}

        # Defensive guard: should never happen since ev_lookup is built from evidence
        phantom = unique_urls - unique_available
        if phantom:
            logger.warning(
                "Writer cited %d URL(s) not in the evidence set", len(phantom)
            )

        diversity = (
            min(1.0, len(unique_urls) / max(1, len(unique_available)))
            if evidence
            else 0.0
        )

        unit = ContentUnit(
            id=f"cu_{uuid.uuid4().hex[:12]}",
            path=path,
            content=content_text,
            citations=citations,
            evidence_map=evidence_map,
            scores=ContentScores(
                confidence=0.0,  # set by verifier
                coverage=0.0,  # set by verifier
                source_diversity=diversity,
            ),
            lineage=lineage
            or ContentLineage(policy_id="", run_id="", engine_version=""),
        )

        return WriterOutput(unit=unit, gaps=gaps, raw_response=raw)


class WriterOutput:
    """Result of a writer invocation."""

    def __init__(
        self,
        unit: ContentUnit | None,
        gaps: list[str],
        raw_response: str,
    ) -> None:
        self.unit = unit
        self.gaps = gaps
        self.raw_response = raw_response

    @property
    def has_content(self) -> bool:
        return self.unit is not None and bool(self.unit.content)

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)
