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

from cce.evidence.formatting import format_evidence_for_prompt
from cce.llm.base import LLMMessage, LLMProvider, LLMResponse
from cce.llm.retry import with_llm_retry
from cce.models.content import (
    Citation,
    ClaimMapping,
    ContentLineage,
    ContentScores,
    ContentUnit,
)
from cce.models.evidence import Evidence
from cce.models.paths import PathConfig
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

STYLE GUIDANCE (light -- the editor handles details):
- Vary sentence length. Mix short fragments with longer constructions.
- Make declarative claims where the evidence is strong. Reserve hedging \
("may", "suggests", "could") for genuinely uncertain claims.

STRUCTURE GUIDANCE (applies to every path):
- Do NOT open with meta-introductions ("In this essay...", "This guide will...", \
"Here we explore...").
- Do NOT emit labelled scaffolding headings such as "Overview", "Introduction", \
"Closing Frame", "Conclusion", or "Summary". Open on substance; end on substance.
- Headings name the actual subject of their section, not its role in the document.

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
        path_config: PathConfig | None = None,
        feedback: str | None = None,
        lineage: ContentLineage | None = None,
        evidence_block: str | None = None,
        ev_lookup: dict[str, Evidence] | None = None,
    ) -> WriterOutput:
        """Produce a draft for one output path from the given evidence.

        Args:
            request: The original curation request.
            evidence: Evidence objects to synthesize from.
            path: Which output path to write for.
            path_config: Optional path-specific overrides for tone, structure,
                         depth, and audience.
            feedback: Optional verifier feedback from a previous iteration
                      (gaps to fill, claims to fix).
            lineage: Provenance metadata to attach to the content unit.
            evidence_block: Pre-formatted writer-style evidence prompt block
                (audit P7). When provided, skips the internal
                ``format_evidence_for_prompt`` call — the caller has already
                paid that cost once for the whole run. None -> fall back to
                computing it here (backward-compat for direct callers).
        """
        if not evidence:
            logger.warning("Writer called with no evidence for path '%s'", path)
            return WriterOutput(
                unit=None,
                gaps=["No evidence provided"],
                raw_response="",
            )

        if evidence_block is None:
            evidence_block = format_evidence_for_prompt(evidence, style="writer")

        # Resolve audience: path config can override the request default
        audience = request.audience
        if path_config is not None and path_config.audience_override:
            audience = path_config.audience_override

        # Resolve subtopics: path config can limit scope
        subtopics = request.subtopics
        if path_config and path_config.subtopic_limit:
            subtopics = request.subtopics[: path_config.subtopic_limit]

        jurisdiction_line = ""
        if request.constraints and request.constraints.jurisdiction:
            jurisdiction_line = (
                f"Jurisdiction/scope: {request.constraints.jurisdiction}\n"
            )

        user_prompt = f"""Topic: {request.topic}
Subtopics: {", ".join(subtopics) if subtopics else "None specified"}
Target audience: {audience}
Output path: {path}
{jurisdiction_line}
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

        # Compose system prompt: base + optional path-specific addendum
        system_prompt = WRITER_SYSTEM_PROMPT
        if path_config is not None:
            system_prompt += self._build_path_addendum(path_config)

        async def _attempt() -> WriterOutput:
            response = await self._llm.complete(
                messages,
                system=system_prompt,
                temperature=0.2,  # low temp for factual consistency; do not increase without testing
            )
            output = self._parse_response(
                response, evidence, path, lineage, ev_lookup=ev_lookup
            )
            output.token_usage = response.usage
            return output

        return await with_llm_retry(_attempt)

    @staticmethod
    def _build_path_addendum(path_config: PathConfig) -> str:
        """Build supplemental writer instructions from PathConfig."""
        parts: list[str] = []

        parts.append(f"\n--- PATH-SPECIFIC GUIDANCE (path: {path_config.id}) ---")
        parts.append(f"Tone: {path_config.tone}")
        parts.append(f"Structure: {path_config.structure}")
        parts.append(f"Depth: {path_config.depth}")

        if path_config.section_requirements:
            parts.append(
                f"Required sections: {', '.join(path_config.section_requirements)}"
            )
        if path_config.max_words:
            parts.append(f"Target length: ~{path_config.max_words} words")
        if path_config.max_paragraphs:
            parts.append(
                f"Structure: ~{path_config.max_paragraphs} substantive paragraphs"
            )
        if path_config.prompt_addendum:
            parts.append(path_config.prompt_addendum)

        parts.append("--- END PATH GUIDANCE ---")
        return "\n".join(parts)

    def _parse_response(
        self,
        response: LLMResponse,
        evidence: list[Evidence],
        path: str,
        lineage: ContentLineage | None,
        *,
        ev_lookup: dict[str, Evidence] | None = None,
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

        # Evidence ID lookup for URL resolution — use the caller's version
        # when provided (audit P8), else build locally. The caller's dict is
        # read-only from here.
        if ev_lookup is None:
            ev_lookup = {ev.id: ev for ev in evidence}

        # Parse citations — warn if LLM cited unknown evidence IDs
        citations_used = parsed.get("citations_used", [])
        citations = []
        for eid in citations_used:
            if eid in ev_lookup:
                citations.append(Citation(evidence_id=eid, url=ev_lookup[eid].url))
            else:
                logger.warning("Writer cited unknown evidence ID: %s", eid)

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
        token_usage: dict | None = None,
    ) -> None:
        self.unit = unit
        self.gaps = gaps
        self.raw_response = raw_response
        self.token_usage: dict = token_usage or {}

    @property
    def has_content(self) -> bool:
        return self.unit is not None and bool(self.unit.content)

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)
