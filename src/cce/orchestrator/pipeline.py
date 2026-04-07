"""Pipeline orchestrator.

Wires the core loop: discover -> store -> write -> verify -> gate -> (loop or publish).
Phase 1 entry point. Single-threaded, no API, no plugins.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from cce.config.types import EngineConfig, QualityGateConfig
from cce.discovery.adapters.base import CrawlAdapter
from cce.discovery.discoverer import Discoverer
from cce.discovery.embeddings import EmbeddingProvider
from cce.evidence.store import EvidenceStore
from cce.llm.base import LLMProvider
from cce.models.content import ContentLineage, ContentScores, ContentUnit
from cce.models.evidence import Evidence
from cce.models.job import Job, JobError, JobProgress, JobStage, JobStatus, StageRecord
from cce.models.package import PackageLineage, PublishPackage
from cce.models.paths import PathConfig
from cce.models.request import CurationRequest
from cce.policy.types import SourcePolicy
from cce.synthesis.writer import Writer
from cce.tagging.base import TaxonomyPlugin, TaxonomyUnavailableError
from cce.verification.gate import GateDecision, GateResult, QualityGate
from cce.verification.verifier import Verifier

logger = logging.getLogger(__name__)


def _terminal_decisions(
    gate_results: list[GateResult],
    paths: list[str],
) -> list[GateDecision]:
    """Extract the terminal (last) gate decision for each output path.

    The write-verify loop may produce multiple intermediate FAIL results
    before reaching a terminal PASS or REVIEW.  For final-status
    determination we only care about the last decision per path — the one
    that actually ended the loop.

    When there are *N* paths, the gate_results list is partitioned into *N*
    consecutive groups (one per path, in order).  Within each group the last
    entry is the terminal decision.
    """
    if not gate_results:
        return []

    # Partition results into per-path groups.  Gate results are appended in
    # path order, with each path contributing >=1 result.  We split by
    # counting how many results belong to each path: the total for a path
    # equals the number of write-verify iterations it ran.
    n_paths = len(paths)

    if n_paths <= 1:
        # Single path — terminal decision is simply the last one.
        return [gate_results[-1].decision]

    # Multiple paths: partition evenly when possible, otherwise split by
    # tracking iteration numbering (iteration resets to 1 per path).
    groups: list[list[GateResult]] = []
    current_group: list[GateResult] = []
    for gr in gate_results:
        if gr.iteration == 1 and current_group:
            groups.append(current_group)
            current_group = []
        current_group.append(gr)
    if current_group:
        groups.append(current_group)

    return [group[-1].decision for group in groups]


class Pipeline:
    """Orchestrates the full curation pipeline."""

    def __init__(
        self,
        config: EngineConfig,
        crawl_adapter: CrawlAdapter,
        evidence_store: EvidenceStore,
        llm: LLMProvider,
        embedding_provider: EmbeddingProvider | None = None,
        taxonomy_plugin: TaxonomyPlugin | None = None,
        path_configs: dict[str, PathConfig] | None = None,
    ) -> None:
        self._config = config
        self._taxonomy_plugin = taxonomy_plugin
        self._path_configs = path_configs or {}
        self._discoverer = Discoverer(
            adapter=crawl_adapter,
            config=config.crawl,
            embedding_provider=embedding_provider,
            embedding_batch_size=config.embedding.batch_size,
        )
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

        # Job-scoped logger — all pipeline logs include job_id
        job_logger = logging.LoggerAdapter(logger, extra={"job_id": job.id})
        job_logger.info("Pipeline run %s started for topic '%s'", run_id, request.topic)

        # Token accumulator — summed across all writer + verifier calls
        token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

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
            stage_start = datetime.now(UTC)

            evidence = await self._discoverer.discover(request, policy)
            job_logger.info("Discovered %d evidence objects", len(evidence))

            job.stages.append(
                StageRecord(
                    stage=JobStage.DISCOVER,
                    started_at=stage_start,
                    completed_at=datetime.now(UTC),
                    metrics=self._discoverer.last_discover_metrics or None,
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

            # --- Stage 1.5: Tag evidence (optional) ---
            if self._taxonomy_plugin is not None:
                tag_start = datetime.now(UTC)
                tags_available = False
                try:
                    results = await self._taxonomy_plugin.tag_many(evidence)
                    tagged: list[Evidence] = []
                    for ev, result in zip(evidence, results, strict=True):
                        tagged.append(
                            ev.model_copy(
                                update={
                                    "tags": result.tags,
                                    "dimension_signals": result.signals,
                                }
                            )
                        )
                    evidence = tagged
                    tags_available = True
                    job_logger.info(
                        "Tagged %d evidence objects with taxonomy", len(evidence)
                    )
                except TaxonomyUnavailableError:
                    job_logger.warning(
                        "Taxonomy plugin unavailable, proceeding without tags"
                    )
                except Exception:
                    job_logger.warning(
                        "Taxonomy plugin raised unexpected error, proceeding without tags",
                        exc_info=True,
                    )

                job.stages.append(
                    StageRecord(
                        stage=JobStage.TAG,
                        started_at=tag_start,
                        completed_at=datetime.now(UTC),
                        metrics={"tags_available": tags_available},
                    )
                )
                if not tags_available:
                    job_logger.warning(
                        "Quality gate running without taxonomy tags — conflict detection may be limited"
                    )

            # --- Stage 2: Store evidence ---
            stage_start = datetime.now(UTC)
            inserted = await self._evidence_store.put_many(evidence)
            job_logger.info(
                "Stored %d new evidence objects (%d duplicates skipped)",
                inserted,
                len(evidence) - inserted,
            )

            # --- Stage 3: Write + Verify loop (per output path) ---
            all_units: list[ContentUnit] = []
            all_gate_results: list[GateResult] = []

            total_paths = len(request.paths)
            for idx, path in enumerate(request.paths):
                job = self._update_job(
                    job,
                    JobStatus.RUNNING,
                    JobStage.WRITE,
                    progress=JobProgress(completed=idx, total=total_paths),
                )
                job_logger.info(
                    "Progress: path %d/%d ('%s')", idx + 1, total_paths, path
                )

                unit, gate_results = await self._write_verify_loop(
                    request=request,
                    evidence=evidence,
                    path=path,
                    gate=gate,
                    lineage=lineage,
                    job=job,
                    job_logger=job_logger,
                    token_usage=token_usage,
                )

                all_gate_results.extend(gate_results)

                if unit is not None:
                    all_units.append(unit)

            # --- Stage 4: Build publish package ---
            job = self._update_job(job, JobStatus.RUNNING, JobStage.PUBLISH)
            stage_start = datetime.now(UTC)

            # Determine final status based on the *terminal* gate decision
            # for each output path.  Intermediate FAIL decisions (which
            # triggered rewrites) are not terminal — only the last result
            # per path matters.
            final_decisions = _terminal_decisions(all_gate_results, request.paths)

            if all(d == GateDecision.PASS for d in final_decisions):
                final_status = JobStatus.COMPLETED
            elif any(d == GateDecision.REVIEW for d in final_decisions):
                final_status = JobStatus.REVIEW_REQUIRED
            else:
                # Gate returned FAIL after max iterations — content needs human review
                final_status = JobStatus.REVIEW_REQUIRED

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
                    completed_at=datetime.now(UTC),
                    metrics={"token_usage": dict(token_usage)},
                )
            )
            job = self._update_job(job, final_status)

            job_logger.info(
                "Pipeline run %s completed: %d units, confidence=%.3f, status=%s, tokens=%s",
                run_id,
                len(all_units),
                avg_confidence,
                final_status.value,
                token_usage,
            )

            return PipelineResult(
                package=package, job=job, gate_results=all_gate_results
            )

        except Exception as e:
            job_logger.exception("Pipeline run %s failed: %s", run_id, e)
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
        job_logger: logging.LoggerAdapter | None = None,
        token_usage: dict | None = None,
    ) -> tuple[ContentUnit | None, list[GateResult]]:
        """Run the writer-verifier loop for a single output path."""
        _log = job_logger or logger
        _tokens = token_usage  # may be None if called outside full pipeline
        gate_results: list[GateResult] = []
        feedback: str | None = None
        unit: ContentUnit | None = None

        gate_config = gate._config
        max_iters = gate_config.max_writer_iterations

        path_config = self._path_configs.get(path)
        ev_lookup = {ev.id: ev for ev in evidence}

        # Per-path evidence cap (keeps full list for tag aggregation)
        path_evidence = evidence
        if path_config and path_config.max_evidence:
            path_evidence = evidence[: path_config.max_evidence]

        for iteration in range(1, max_iters + 1):
            _log.info(
                "Path '%s': write-verify iteration %d/%d", path, iteration, max_iters
            )

            # Write
            write_start = datetime.now(UTC)
            writer_output = await self._writer.write(
                request=request,
                evidence=path_evidence,
                path=path,
                path_config=path_config,
                feedback=feedback,
                lineage=lineage,
            )

            # Accumulate token usage from writer
            if _tokens and writer_output.token_usage:
                for key in _tokens:
                    _tokens[key] += writer_output.token_usage.get(key, 0)

            if not writer_output.has_content:
                _log.warning("Writer produced no content for path '%s'", path)
                break

            unit = writer_output.unit

            if job is not None:
                job.stages.append(
                    StageRecord(
                        stage=JobStage.WRITE,
                        started_at=write_start,
                        completed_at=datetime.now(UTC),
                    )
                )

            # Verify
            verify_start = datetime.now(UTC)
            jurisdiction = (
                request.constraints.jurisdiction if request.constraints else None
            )
            report = await self._verifier.verify(
                unit, path_evidence, jurisdiction=jurisdiction
            )

            # Accumulate token usage from verifier
            if _tokens and report.token_usage:
                for key in _tokens:
                    _tokens[key] += report.token_usage.get(key, 0)

            if job is not None:
                job.stages.append(
                    StageRecord(
                        stage=JobStage.VERIFY,
                        started_at=verify_start,
                        completed_at=datetime.now(UTC),
                    )
                )

            # Aggregate tags from cited evidence
            cited_ids = {c.evidence_id for c in unit.citations}
            aggregated_tags = sorted(
                {
                    tag
                    for eid in cited_ids
                    if eid in ev_lookup
                    for tag in ev_lookup[eid].tags
                }
            )

            # Update unit scores from verification
            unit = ContentUnit(
                id=unit.id,
                path=unit.path,
                tags=aggregated_tags,
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
            gate_result = gate.evaluate(unit, report, iteration, evidence=path_evidence)
            gate_results.append(gate_result)

            if gate_result.should_publish:
                _log.info("Path '%s': PASSED at iteration %d", path, iteration)
                return unit, gate_results

            if gate_result.should_rewrite:
                feedback = gate_result.feedback
                _log.info("Path '%s': rewriting (iteration %d)", path, iteration)
                continue

            if gate_result.needs_human:
                _log.info(
                    "Path '%s': routed to human review at iteration %d", path, iteration
                )
                return unit, gate_results

        # Exhausted iterations without passing
        _log.info("Path '%s': exhausted %d iterations", path, max_iters)
        return unit, gate_results

    @staticmethod
    def _update_job(
        job: Job,
        status: JobStatus,
        stage: JobStage | None = None,
        error_msg: str | None = None,
        progress: JobProgress | None = None,
    ) -> Job:
        """Update job tracking fields."""
        job.status = status
        job.updated_at = datetime.now(UTC)

        if stage is not None:
            job.stage = stage

        if progress is not None:
            job.progress = progress

        if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.REVIEW_REQUIRED):
            job.completed_at = datetime.now(UTC)

        if error_msg:
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
