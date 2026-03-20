"""Pipeline orchestrator.

Wires the core loop: discover -> store -> write -> verify -> gate -> (loop or publish).
Phase 1 entry point. Single-threaded, no API, no plugins.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from cce.config.types import EngineConfig, QualityGateConfig
from cce.discovery.adapters.base import CrawlAdapter
from cce.discovery.discoverer import Discoverer
from cce.evidence.store import EvidenceStore
from cce.llm.base import LLMProvider
from cce.models.content import ContentLineage, ContentScores, ContentUnit
from cce.models.evidence import Evidence
from cce.models.job import Job, JobProgress, JobStage, JobStatus, StageRecord
from cce.models.package import PackageLineage, PublishPackage
from cce.models.request import CurationRequest
from cce.policy.types import SourcePolicy
from cce.synthesis.writer import Writer
from cce.verification.gate import GateDecision, GateResult, QualityGate
from cce.verification.verifier import Verifier

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrates the full curation pipeline."""

    def __init__(
        self,
        config: EngineConfig,
        crawl_adapter: CrawlAdapter,
        evidence_store: EvidenceStore,
        llm: LLMProvider,
    ) -> None:
        self._config = config
        self._discoverer = Discoverer(adapter=crawl_adapter, config=config.crawl)
        self._evidence_store = evidence_store
        self._writer = Writer(llm=llm)
        self._verifier = Verifier(llm=llm)

    async def run(
        self,
        request: CurationRequest,
        policy: SourcePolicy,
    ) -> PipelineResult:
        """Execute the full pipeline for a curation request.

        Returns a PipelineResult containing the PublishPackage (if successful),
        the Job tracking object, and any gate results from the verification loop.
        """
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        job = Job(id=f"job_{uuid.uuid4().hex[:12]}", request=request)

        logger.info("Pipeline run %s started for topic '%s'", run_id, request.topic)

        # Resolve quality gate config for the risk profile
        gate_config = self._config.quality_gate.get(
            request.risk_profile,
            self._config.quality_gate.get("medium", QualityGateConfig()),
        )
        gate = QualityGate(gate_config)

        lineage = ContentLineage(
            policy_id=request.policy_id,
            taxonomy_id=request.taxonomy_id or "",
            path_config_id=request.path_config_id or "",
            run_id=run_id,
            engine_version=self._config.engine_version,
        )

        try:
            # --- Stage 1: Discover ---
            job = self._update_job(job, JobStatus.RUNNING, JobStage.DISCOVER)
            stage_start = datetime.now(timezone.utc)

            evidence = await self._discoverer.discover(request, policy)
            logger.info("Discovered %d evidence objects", len(evidence))

            job.stages.append(
                StageRecord(
                    stage=JobStage.DISCOVER,
                    started_at=stage_start,
                    completed_at=datetime.now(timezone.utc),
                )
            )

            if not evidence:
                return PipelineResult(
                    package=None,
                    job=self._update_job(
                        job, JobStatus.FAILED, error_msg="No evidence discovered"
                    ),
                    gate_results=[],
                )

            # --- Stage 2: Store evidence ---
            stage_start = datetime.now(timezone.utc)
            inserted = await self._evidence_store.put_many(evidence)
            logger.info(
                "Stored %d new evidence objects (%d duplicates skipped)",
                inserted,
                len(evidence) - inserted,
            )

            # --- Stage 3: Write + Verify loop (per output path) ---
            all_units: list[ContentUnit] = []
            all_gate_results: list[GateResult] = []

            for path in request.paths:
                job = self._update_job(job, JobStatus.RUNNING, JobStage.WRITE)

                unit, gate_results = await self._write_verify_loop(
                    request=request,
                    evidence=evidence,
                    path=path,
                    gate=gate,
                    lineage=lineage,
                    job=job,
                )

                all_gate_results.extend(gate_results)

                if unit is not None:
                    all_units.append(unit)

            # --- Stage 4: Build publish package ---
            job = self._update_job(job, JobStatus.RUNNING, JobStage.PUBLISH)
            stage_start = datetime.now(timezone.utc)

            # Determine final status based on gate results
            final_decisions = [gr.decision for gr in all_gate_results]

            if all(d == GateDecision.PASS for d in final_decisions):
                final_status = JobStatus.COMPLETED
            elif any(d == GateDecision.REVIEW for d in final_decisions):
                final_status = JobStatus.REVIEW_REQUIRED
            else:
                final_status = JobStatus.COMPLETED  # best-effort with what we have

            # Aggregate scores
            if all_units:
                avg_confidence = sum(u.scores.confidence for u in all_units) / len(
                    all_units
                )
                avg_coverage = sum(u.scores.coverage for u in all_units) / len(
                    all_units
                )
                avg_diversity = sum(u.scores.source_diversity for u in all_units) / len(
                    all_units
                )
            else:
                avg_confidence = avg_coverage = avg_diversity = 0.0

            package = PublishPackage(
                job_id=job.id,
                units=all_units,
                evidence=evidence,
                scores=ContentScores(
                    confidence=round(avg_confidence, 3),
                    coverage=round(avg_coverage, 3),
                    source_diversity=round(avg_diversity, 3),
                ),
                lineage=PackageLineage(
                    policy_id=request.policy_id,
                    taxonomy_id=request.taxonomy_id or "",
                    path_config_id=request.path_config_id or "",
                    run_id=run_id,
                    engine_version=self._config.engine_version,
                    stages=job.stages,
                ),
            )

            job.stages.append(
                StageRecord(
                    stage=JobStage.PUBLISH,
                    started_at=stage_start,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            job = self._update_job(job, final_status)

            logger.info(
                "Pipeline run %s completed: %d units, confidence=%.3f, status=%s",
                run_id,
                len(all_units),
                avg_confidence,
                final_status.value,
            )

            return PipelineResult(
                package=package, job=job, gate_results=all_gate_results
            )

        except Exception as e:
            logger.exception("Pipeline run %s failed: %s", run_id, e)
            return PipelineResult(
                package=None,
                job=self._update_job(job, JobStatus.FAILED, error_msg=str(e)),
                gate_results=[],
            )

    async def _write_verify_loop(
        self,
        request: CurationRequest,
        evidence: list[Evidence],
        path: str,
        gate: QualityGate,
        lineage: ContentLineage,
        job: Job | None = None,
    ) -> tuple[ContentUnit | None, list[GateResult]]:
        """Run the writer-verifier loop for a single output path."""
        gate_results: list[GateResult] = []
        feedback: str | None = None
        unit: ContentUnit | None = None

        gate_config = gate._config
        max_iters = gate_config.max_writer_iterations

        for iteration in range(1, max_iters + 1):
            logger.info(
                "Path '%s': write-verify iteration %d/%d", path, iteration, max_iters
            )

            # Write
            write_start = datetime.now(timezone.utc)
            writer_output = await self._writer.write(
                request=request,
                evidence=evidence,
                path=path,
                feedback=feedback,
                lineage=lineage,
            )

            if not writer_output.has_content:
                logger.warning("Writer produced no content for path '%s'", path)
                break

            unit = writer_output.unit

            if job is not None:
                job.stages.append(
                    StageRecord(
                        stage=JobStage.WRITE,
                        started_at=write_start,
                        completed_at=datetime.now(timezone.utc),
                    )
                )

            # Verify
            verify_start = datetime.now(timezone.utc)
            report = await self._verifier.verify(unit, evidence)

            if job is not None:
                job.stages.append(
                    StageRecord(
                        stage=JobStage.VERIFY,
                        started_at=verify_start,
                        completed_at=datetime.now(timezone.utc),
                    )
                )

            # Update unit scores from verification
            unit = ContentUnit(
                id=unit.id,
                path=unit.path,
                tags=unit.tags,
                content=unit.content,
                citations=unit.citations,
                evidence_map=unit.evidence_map,
                scores=ContentScores(
                    confidence=report.confidence_score,
                    coverage=report.pass_rate,
                    source_diversity=unit.scores.source_diversity,
                ),
                lineage=unit.lineage,
            )

            # Gate decision
            gate_result = gate.evaluate(unit, report, iteration)
            gate_results.append(gate_result)

            if gate_result.should_publish:
                logger.info("Path '%s': PASSED at iteration %d", path, iteration)
                return unit, gate_results

            if gate_result.should_rewrite:
                feedback = gate_result.feedback
                logger.info("Path '%s': rewriting (iteration %d)", path, iteration)
                continue

            if gate_result.needs_human:
                logger.info(
                    "Path '%s': routed to human review at iteration %d", path, iteration
                )
                return unit, gate_results

        # Exhausted iterations without passing
        logger.info("Path '%s': exhausted %d iterations", path, max_iters)
        return unit, gate_results

    @staticmethod
    def _update_job(
        job: Job,
        status: JobStatus,
        stage: JobStage | None = None,
        error_msg: str | None = None,
    ) -> Job:
        """Update job tracking fields."""
        job.status = status
        job.updated_at = datetime.now(timezone.utc)

        if stage is not None:
            job.stage = stage

        if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.REVIEW_REQUIRED):
            job.completed_at = datetime.now(timezone.utc)

        if error_msg:
            from cce.models.job import JobError

            job.error = JobError(
                code="pipeline_error",
                message=error_msg,
                stage=job.stage or JobStage.DISCOVER,
            )

        return job


class PipelineResult:
    """Result of a full pipeline run."""

    def __init__(
        self,
        package: PublishPackage | None,
        job: Job,
        gate_results: list[GateResult],
    ) -> None:
        self.package = package
        self.job = job
        self.gate_results = gate_results

    @property
    def succeeded(self) -> bool:
        return self.package is not None and self.job.status == JobStatus.COMPLETED

    @property
    def needs_review(self) -> bool:
        return self.job.status == JobStatus.REVIEW_REQUIRED

    @property
    def failed(self) -> bool:
        return self.job.status == JobStatus.FAILED
