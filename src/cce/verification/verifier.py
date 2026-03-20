"""Verifier agent.

The verifier is the critic role -- separate from the writer. It receives
a draft ContentUnit and the evidence store, and checks:

1. Every claim has at least one citation
2. Every citation resolves to a stored evidence excerpt
3. The cited evidence actually supports the claim
4. Contradictions between sources are identified
5. No "evidence leakage" (claims not backed by provided evidence)

The verifier outputs a VerificationReport with per-claim results and
an aggregate confidence score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cce.llm.base import LLMMessage, LLMProvider
from cce.models.content import ContentUnit
from cce.models.evidence import Evidence
from cce.parsing import extract_json

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM_PROMPT = """\
You are a rigorous fact-checking verifier. Your job is to verify that every \
claim in the draft content is supported by the provided evidence excerpts.

For EACH paragraph in the content, you must:
1. Identify every factual claim (statements of fact, statistics, dates, etc.)
2. Check whether the claim has a citation [ev:ID]
3. Check whether the cited evidence actually supports the claim
4. Flag any claims that appear to come from the LLM's training data rather \
than the provided evidence (evidence leakage)
5. Identify contradictions between cited sources

ASSESSMENT CATEGORIES for each claim:
- "supported": Claim has a citation and the evidence supports it
- "unsupported": Claim has a citation but the evidence doesn't actually say this
- "uncited": Claim is a factual statement with no citation
- "leakage": Claim introduces specific facts/data not in any provided evidence
- "conflict": Multiple sources contradict each other on this claim
- "gap_acknowledged": Content correctly marks insufficient evidence

OUTPUT FORMAT:
Return a JSON object:
{
  "claims": [
    {
      "claim": "<the claim text>",
      "citation_ids": ["ev_id1"],
      "assessment": "supported|unsupported|uncited|leakage|conflict|gap_acknowledged",
      "explanation": "<why this assessment>",
      "suggestion": "<how to fix, if not supported>"
    },
    ...
  ],
  "summary": {
    "total_claims": <int>,
    "supported": <int>,
    "unsupported": <int>,
    "uncited": <int>,
    "leakage": <int>,
    "conflicts": <int>,
    "gaps_acknowledged": <int>
  },
  "overall_feedback": "<summary of what needs fixing for the writer>",
  "contradictions": [
    {"topic": "<what they disagree about>", "evidence_ids": ["ev_1", "ev_2"]}
  ]
}

Be strict. If a claim contains specific numbers, dates, or named entities, \
it MUST be supported by the evidence. General framing and transitions do not \
count as factual claims and do not need citations.\
"""


@dataclass
class ClaimVerification:
    """Result of verifying a single claim."""

    claim: str
    citation_ids: list[str] = field(default_factory=list)
    assessment: str = (
        ""  # supported, unsupported, uncited, leakage, conflict, gap_acknowledged
    )
    explanation: str = ""
    suggestion: str = ""


@dataclass
class Contradiction:
    """A contradiction found between evidence sources."""

    topic: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    """Full verification report for a content unit."""

    claims: list[ClaimVerification] = field(default_factory=list)
    total_claims: int = 0
    supported: int = 0
    unsupported: int = 0
    uncited: int = 0
    leakage: int = 0
    conflicts: int = 0
    gaps_acknowledged: int = 0
    contradictions: list[Contradiction] = field(default_factory=list)
    overall_feedback: str = ""
    confidence_score: float = 0.0
    raw_response: str = ""

    @property
    def pass_rate(self) -> float:
        """Fraction of claims that are supported or acknowledged gaps."""
        passing = self.supported + self.gaps_acknowledged
        return passing / max(1, self.total_claims)


class Verifier:
    """Fact-checking verifier agent."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def verify(
        self,
        unit: ContentUnit,
        evidence: list[Evidence],
    ) -> VerificationReport:
        """Verify a content unit against its evidence.

        Args:
            unit: The draft content to verify.
            evidence: All evidence objects available for this curation run.
        """
        if not unit.content:
            return VerificationReport(
                overall_feedback="Empty content -- nothing to verify"
            )

        # Build evidence reference for the verifier
        evidence_block = self._format_evidence(evidence)

        user_prompt = f"""=== DRAFT CONTENT TO VERIFY ===
{unit.content}
=== END DRAFT ===

=== EVIDENCE AVAILABLE (the ONLY acceptable sources) ===
{evidence_block}
=== END EVIDENCE ===

Verify every factual claim in the draft against the evidence provided. \
Any claim containing specific facts, data, or assertions that cannot be \
traced to the evidence above should be flagged.
"""

        messages = [LLMMessage(role="user", content=user_prompt)]

        logger.info(
            "Verifier: checking content unit %s (%d evidence objects)",
            unit.id,
            len(evidence),
        )

        response = await self._llm.complete(
            messages,
            system=VERIFIER_SYSTEM_PROMPT,
            temperature=0.1,  # very low temp for consistent judgment
            max_tokens=16384,
        )

        return self._parse_response(response.content)

    def _parse_response(self, raw: str) -> VerificationReport:
        """Parse verifier LLM response into a structured report."""
        parsed = extract_json(raw)

        if parsed is None:
            logger.warning("Verifier response was not valid JSON")
            return VerificationReport(
                overall_feedback="Verifier produced unstructured output -- manual review needed",
                confidence_score=0.0,
                raw_response=raw,
            )

        # Parse individual claims
        claims: list[ClaimVerification] = []
        for item in parsed.get("claims", []):
            claims.append(
                ClaimVerification(
                    claim=item.get("claim", ""),
                    citation_ids=item.get("citation_ids", []),
                    assessment=item.get("assessment", ""),
                    explanation=item.get("explanation", ""),
                    suggestion=item.get("suggestion", ""),
                )
            )

        # Parse summary counts
        summary = parsed.get("summary", {})
        total = summary.get("total_claims", len(claims))
        supported = summary.get("supported", 0)
        unsupported = summary.get("unsupported", 0)
        uncited = summary.get("uncited", 0)
        leakage = summary.get("leakage", 0)
        conflicts = summary.get("conflicts", 0)
        gaps = summary.get("gaps_acknowledged", 0)

        # Parse contradictions
        contradictions = [
            Contradiction(
                topic=c.get("topic", ""),
                evidence_ids=c.get("evidence_ids", []),
            )
            for c in parsed.get("contradictions", [])
        ]

        # Calculate confidence score
        # supported + gaps = good, everything else reduces confidence
        passing = supported + gaps
        confidence = passing / max(1, total)

        # Penalize leakage more heavily -- it's the worst failure mode
        if leakage > 0:
            confidence *= max(0.0, 1.0 - (leakage / max(1, total)) * 1.5)

        # Penalize contradictions
        if conflicts > 0:
            confidence *= 0.9

        return VerificationReport(
            claims=claims,
            total_claims=total,
            supported=supported,
            unsupported=unsupported,
            uncited=uncited,
            leakage=leakage,
            conflicts=conflicts,
            gaps_acknowledged=gaps,
            contradictions=contradictions,
            overall_feedback=parsed.get("overall_feedback", ""),
            confidence_score=round(min(1.0, max(0.0, confidence)), 3),
            raw_response=raw,
        )

    @staticmethod
    def _format_evidence(evidence: list[Evidence]) -> str:
        """Format evidence for the verifier prompt."""
        lines: list[str] = []
        for ev in evidence:
            lines.append(f"[{ev.id}] (URL: {ev.url})")
            lines.append(ev.excerpt)
            lines.append("")
        return "\n".join(lines)
