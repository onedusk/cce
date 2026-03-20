"""Quality gate.

Consumes the verifier's VerificationReport and makes a routing decision:
pass (autopublish), fail (return to writer with feedback), or review
(below threshold, needs human eyes).

The gate's thresholds are driven by the risk profile in EngineConfig.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from cce.config.types import QualityGateConfig
from cce.models.content import ContentScores, ContentUnit
from cce.verification.verifier import VerificationReport

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
    ) -> GateResult:
        """Decide whether to pass, fail, or route to review.

        Decision logic:
        1. If confidence >= autopublish_threshold AND citation density is met -> PASS
        2. If we haven't hit max iterations AND there are fixable issues -> FAIL (rewrite)
        3. Otherwise -> REVIEW (needs human)
        """
        confidence = report.confidence_score
        coverage = report.pass_rate

        # Check citation density per paragraph
        citation_ok, citation_ratio = self._check_citation_density(unit)

        # Build feedback for the writer
        feedback_parts: list[str] = []

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

        feedback = "\n".join(feedback_parts) if feedback_parts else "No issues found."

        # Decision logic
        if (
            confidence >= self._config.autopublish_threshold
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
        elif (
            iteration < self._config.max_writer_iterations
            and self._has_fixable_issues(report)
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

        # Only check substantive paragraphs (>15 words)
        substantive = [p for p in paragraphs if len(p.split()) > 15]
        if not substantive:
            return True, 1.0

        import re

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
