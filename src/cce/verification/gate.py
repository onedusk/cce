"""Quality gate.

Consumes the verifier's VerificationReport and makes a routing decision:
pass (autopublish), fail (return to writer with feedback), or review
(below threshold, needs human eyes).

The gate's thresholds are driven by the risk profile in EngineConfig.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from cce.config.types import QualityGateConfig
from cce.models.content import ContentUnit
from cce.models.evidence import Evidence
from cce.parsing import EV_MARKER_RE, resolve_evidence_id
from cce.verification.verifier import VerificationReport

MIN_SUBSTANTIVE_WORDS = 15  # min words for a paragraph to be checked for citations

logger = logging.getLogger(__name__)


class GateDecision(Enum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


@dataclass
class GateResult:
    """Output of the quality gate."""

    decision: GateDecision
    confidence: float
    coverage: float
    feedback: (
        str  # actionable feedback for the writer (if FAIL) or reviewer (if REVIEW)
    )
    report: VerificationReport
    iteration: int  # which writer-verifier loop iteration this is

    @property
    def should_rewrite(self) -> bool:
        return self.decision == GateDecision.FAIL

    @property
    def should_publish(self) -> bool:
        return self.decision == GateDecision.PASS

    @property
    def needs_human(self) -> bool:
        return self.decision == GateDecision.REVIEW


class QualityGate:
    """Evaluates verification reports against configured thresholds."""

    def __init__(self, config: QualityGateConfig) -> None:
        self._config = config

    def evaluate(
        self,
        unit: ContentUnit,
        report: VerificationReport,
        iteration: int,
        evidence: list[Evidence],
    ) -> GateResult:
        """Decide whether to pass, fail, or route to review.

        Decision logic:
        1. If every inline citation marker resolves to ``evidence`` AND
           confidence >= autopublish_threshold AND citation density is met -> PASS
        2. If we haven't hit max iterations AND there are fixable issues
           (including unresolved markers) -> FAIL (rewrite)
        3. Otherwise -> REVIEW (needs human)

        ``evidence`` is the set the draft was written and verified against
        (the pipeline passes the path's evidence, a subset of what emit
        resolves against). It is required: a marker is only checkable
        against a known set (B4).
        """
        confidence = report.confidence_score
        coverage = report.pass_rate

        # Every inline marker must resolve to provided evidence (B4) — the
        # density check below counts markers, so phantom IDs could otherwise
        # meet the threshold while citing nothing real.
        unresolved = self._unresolved_markers(unit, evidence)

        # Check citation density per paragraph
        citation_ok, citation_ratio = self._check_citation_density(unit)

        # Build feedback for the writer
        feedback_parts: list[str] = []

        # A bracket holding several IDs never resolves (emit renders [^?])
        # even when each ID is real, so it gets its own actionable line.
        multi_id = [m for m in unresolved if "," in m]
        unknown = [m for m in unresolved if "," not in m]
        if unknown:
            feedback_parts.append(
                f"{len(unknown)} citation marker(s) do not resolve to any "
                f"provided evidence: {', '.join(unknown)}. Cite only evidence "
                f"IDs listed in the evidence block."
            )
        if multi_id:
            feedback_parts.append(
                f"{len(multi_id)} citation marker(s) put several IDs in one "
                f"bracket: {' '.join(f'[{m}]' for m in multi_id)}. Use one marker "
                f"per source, e.g. [ev:ID1][ev:ID2]."
            )

        if report.unsupported > 0:
            feedback_parts.append(
                f"{report.unsupported} claim(s) have citations that don't match "
                f"the evidence. Fix or remove these claims."
            )

        if report.uncited > 0:
            feedback_parts.append(
                f"{report.uncited} factual claim(s) have no citations. "
                f"Add [ev:ID] citations or mark as [INSUFFICIENT EVIDENCE]."
            )

        if report.leakage > 0:
            feedback_parts.append(
                f"{report.leakage} claim(s) appear to come from training data, "
                f"not from provided evidence. Remove these or find supporting evidence."
            )

        if report.conflicts > 0:
            feedback_parts.append(
                f"{report.conflicts} contradiction(s) between sources found. "
                f"Explicitly acknowledge conflicts and cite both sides."
            )

        if not citation_ok:
            feedback_parts.append(
                f"Citation density: {citation_ratio:.0%} of substantive paragraphs have "
                f">= {self._config.min_citations_per_paragraph} citation(s) "
                f"(need {self._config.min_citation_density_ratio:.0%})."
            )

        if evidence:
            coi_count = sum(
                1
                for ev in evidence
                if ev.source_quality and ev.source_quality.conflict_of_interest
            )
            if coi_count > 0:
                logger.warning(
                    "Gate: %d evidence source(s) flagged as potential conflict of "
                    "interest (marketing/sponsored content)",
                    coi_count,
                )

        feedback = "\n".join(feedback_parts) if feedback_parts else "No issues found."

        # Decision logic
        if (
            not unresolved
            and confidence >= self._config.autopublish_threshold
            and citation_ok
            and report.leakage == 0
        ):
            decision = GateDecision.PASS
            logger.info(
                "Gate PASS: confidence=%.3f (threshold=%.2f), iteration=%d",
                confidence,
                self._config.autopublish_threshold,
                iteration,
            )
        elif iteration < self._config.max_writer_iterations and (
            bool(unresolved) or self._has_fixable_issues(report)
        ):
            decision = GateDecision.FAIL
            logger.info(
                "Gate FAIL (rewrite): confidence=%.3f, fixable issues found, iteration=%d/%d",
                confidence,
                iteration,
                self._config.max_writer_iterations,
            )
        else:
            decision = GateDecision.REVIEW
            if iteration >= self._config.max_writer_iterations:
                feedback += (
                    f"\nMax iterations ({self._config.max_writer_iterations}) reached. "
                    f"Routing to human review."
                )
            logger.info(
                "Gate REVIEW: confidence=%.3f, iteration=%d/%d",
                confidence,
                iteration,
                self._config.max_writer_iterations,
            )

        return GateResult(
            decision=decision,
            confidence=confidence,
            coverage=coverage,
            feedback=feedback,
            report=report,
            iteration=iteration,
        )

    @staticmethod
    def _unresolved_markers(unit: ContentUnit, evidence: list[Evidence]) -> list[str]:
        """Marker IDs in ``unit.content`` that match no evidence, in order of
        first appearance. Uses emit's grammar and prefix fallback
        (``cce.parsing``), so a marker the gate accepts is one emit resolves."""
        by_id = {ev.id: ev for ev in evidence}
        unresolved: list[str] = []
        for match in EV_MARKER_RE.finditer(unit.content):
            raw = match.group(1) or match.group(2)
            _, resolved = resolve_evidence_id(raw, by_id)
            if resolved is None and raw not in unresolved:
                unresolved.append(raw)
        return unresolved

    def _check_citation_density(self, unit: ContentUnit) -> tuple[bool, float]:
        """Check citation density. Returns (passes, ratio of paragraphs meeting threshold)."""
        if not unit.content:
            return False, 0.0

        # Split content into paragraphs (skip headings and empty lines)
        paragraphs = [
            p.strip()
            for p in unit.content.split("\n\n")
            if p.strip() and not p.strip().startswith("#")
        ]

        # Only check substantive paragraphs
        substantive = [p for p in paragraphs if len(p.split()) > MIN_SUBSTANTIVE_WORDS]
        if not substantive:
            return True, 1.0

        passing = sum(
            1
            for p in substantive
            if len(re.findall(r"\[ev[_:][^\]]+\]", p))
            >= self._config.min_citations_per_paragraph
        )

        ratio = passing / len(substantive)
        return ratio >= self._config.min_citation_density_ratio, ratio

    @staticmethod
    def _has_fixable_issues(report: VerificationReport) -> bool:
        """Are there issues the writer can plausibly fix in another iteration?"""
        # Uncited and unsupported claims are fixable (add/fix citations)
        # Leakage is fixable (remove fabricated claims)
        # Contradictions are somewhat fixable (acknowledge them)
        return (
            report.unsupported > 0
            or report.uncited > 0
            or report.leakage > 0
            or report.conflicts > 0
        )
