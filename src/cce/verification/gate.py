"""Quality gate.

Consumes the verifier's VerificationReport and makes a routing decision:
pass (a quality signal — whether a passed job publishes is the publish
policy's call, B8), fail (return to writer with feedback), or review
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

# The writer's gap marker, in the grammar emit strips from the page
# (output/mdx _GAP_RE), so a draft the gate calls empty is one emit would
# write as an empty page.
_GAP_MARKER_RE = re.compile(r"\[INSUFFICIENT EVIDENCE:\s*([^\]]*)\]", re.DOTALL)

logger = logging.getLogger(__name__)


# A heading is 1-6 "#" followed by whitespace or the end of the line
# (CommonMark): "#1 cause of insomnia" is prose.
_HEADING_LINE_RE = re.compile(r"#{1,6}(\s|$)")


def _strip_headings(text: str) -> str:
    """``text`` without its markdown heading lines."""
    return "\n".join(
        line for line in text.split("\n") if not _HEADING_LINE_RE.match(line.strip())
    )


def _published_text(content: str) -> str:
    """The body as emit publishes it: gap markers removed. A citation the
    writer put inside a gap marker goes with it, so it can't count here."""
    return _GAP_MARKER_RE.sub("", content)


def _resolved_ids(text: str, by_id: dict[str, Evidence]) -> set[str]:
    """Canonical IDs of the markers in ``text`` that resolve to evidence."""
    ids: set[str] = set()
    for match in EV_MARKER_RE.finditer(text):
        ev_id, resolved = resolve_evidence_id(match.group(1) or match.group(2), by_id)
        if resolved is not None:
            ids.add(ev_id)
    return ids


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
        1. If every inline citation marker resolves to ``evidence`` AND at
           least one does AND the draft has text besides headings and gap
           markers AND confidence >= pass_threshold AND citation density is
           met -> PASS
        2. If we haven't hit max iterations AND there are fixable issues
           (including unresolved markers, a draft with no citation or no
           text, and a citation density miss) -> FAIL (rewrite)
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

        # No citation, no ship: a draft with no marker that resolves, or with
        # nothing left once headings and gap markers are removed, never
        # passes, whatever the verifier scored (a gap-only draft is all
        # gap_acknowledged, confidence 1.0).
        # Both read the text emit publishes, not the raw draft.
        published = _published_text(unit.content)
        uncited_draft = not _resolved_ids(published, {ev.id: ev for ev in evidence})
        empty_draft = not EV_MARKER_RE.sub("", _strip_headings(published)).strip()

        # Check citation density per paragraph
        citation_ok, citation_ratio = self._check_citation_density(unit, evidence)

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

        if empty_draft:
            feedback_parts.append(
                "The draft has no text once headings and [INSUFFICIENT EVIDENCE] "
                "markers are removed. Write what the evidence supports and cite "
                "it with [ev:ID] markers."
            )
        elif uncited_draft and not unresolved:
            feedback_parts.append(
                "The draft has no citation marker that resolves to provided "
                "evidence. Cite the evidence behind each claim with [ev:ID] "
                "markers."
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
            and not uncited_draft
            and not empty_draft
            and confidence >= self._config.pass_threshold
            and citation_ok
            and report.leakage == 0
        ):
            decision = GateDecision.PASS
            logger.info(
                "Gate PASS: confidence=%.3f (threshold=%.2f), iteration=%d",
                confidence,
                self._config.pass_threshold,
                iteration,
            )
        elif iteration < self._config.max_writer_iterations and (
            bool(unresolved)
            or uncited_draft
            or empty_draft
            or not citation_ok
            or self._has_fixable_issues(report)
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

    def _check_citation_density(
        self, unit: ContentUnit, evidence: list[Evidence]
    ) -> tuple[bool, float]:
        """Check citation density. Returns (passes, ratio of paragraphs meeting threshold).

        A paragraph meets the threshold on distinct evidence IDs that resolve
        to ``evidence``: the same marker twice counts once.
        """
        if not unit.content:
            return False, 0.0

        # Split content into paragraphs (skip heading lines and empty blocks).
        # Only the heading lines go: the text under a heading in the same
        # block is checked like any other paragraph.
        paragraphs = [
            p
            for p in (
                _strip_headings(block).strip()
                for block in _published_text(unit.content).split("\n\n")
            )
            if p
        ]

        # Only check substantive paragraphs
        substantive = [p for p in paragraphs if len(p.split()) > MIN_SUBSTANTIVE_WORDS]
        if not substantive:
            return True, 1.0

        by_id = {ev.id: ev for ev in evidence}
        passing = sum(
            1
            for p in substantive
            if len(_resolved_ids(p, by_id)) >= self._config.min_citations_per_paragraph
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
