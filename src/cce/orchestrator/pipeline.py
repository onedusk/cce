"""Pipeline orchestrator.

Wires the core loop: discover -> store -> write -> verify -> gate -> (loop or publish).
Phase 1 entry point. Single-threaded at the process level; per-path
writer/verifier loops fan out concurrently via asyncio.gather (audit P1).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from cce.config.types import EngineConfig, QualityGateConfig
from cce.discovery.adapters.base import CrawlAdapter
from cce.discovery.discoverer import Discoverer
from cce.discovery.embeddings import EmbeddingProvider
from cce.evidence.formatting import format_evidence_for_prompt
from cce.evidence.store import EvidenceStore
from cce.llm.base import LLMProvider
from cce.models.content import ContentLineage, ContentScores, ContentUnit
from cce.models.evidence import Evidence
from cce.models.job import Job, JobError, JobProgress, JobStage, JobStatus, StageRecord
from cce.models.package import PackageLineage, PublishPackage
from cce.models.paths import PathConfig
from cce.models.request import CurationRequest
from cce.policy.types import SourcePolicy
from cce.synthesis.scoring import Scorer
from cce.synthesis.writer import Writer
from cce.tagging.base import TaxonomyPlugin, TaxonomyUnavailableError
from cce.verification.gate import GateDecision, GateResult, QualityGate
from cce.verification.verifier import Verifier

logger = logging.getLogger(__name__)


# --- Token-usage helpers (audit P1) ---------------------------------------
# Each per-path task owns its own dict to avoid contention during LLM calls;
# the parent merges them after asyncio.gather returns.

_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _zero_tokens() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_KEYS, 0)


def _merge_tokens(into: dict[str, int], frm: Mapping[str, int]) -> None:
    for k in _TOKEN_KEYS:
        into[k] += int(frm.get(k, 0))


def _format_completion_line(
    *,
    token_usage: Mapping[str, int],
    path_count: int,
    per_path_iterations: list[int],
    cost_estimate_usd: float | None = None,
) -> str:
    """Single-line structured pipeline-completion summary (audit D3).

    Format:
        Pipeline complete: input=N, (cache_read=N, cache_write=N), output=N[, est_cost=$N], paths=N, iterations=[...]

    Numbers use thousands separators so a 50k-token run reads as 50,000.
    ``cost_estimate_usd=None`` (the default in this sprint — no pricing
    table is wired yet) drops the est_cost field entirely.
    """
    parts = [
        f"input={token_usage.get('input_tokens', 0):,}",
        f"(cache_read={token_usage.get('cache_read_input_tokens', 0):,}, "
        f"cache_write={token_usage.get('cache_creation_input_tokens', 0):,})",
        f"output={token_usage.get('output_tokens', 0):,}",
    ]
    if cost_estimate_usd is not None:
        parts.append(f"est_cost=${cost_estimate_usd:.4f}")
    parts.append(f"paths={path_count}")
    parts.append(f"iterations={per_path_iterations}")
    return "Pipeline complete: " + ", ".join(parts)


def _per_path_iteration_counts(job: Job, paths: list[str]) -> list[int]:
    """Max WRITE iteration number reached per path, in `paths` order."""
    by_path: dict[str, int] = dict.fromkeys(paths, 0)
    for rec in job.stages:
        if rec.stage != JobStage.WRITE or not rec.metrics:
            continue
        p = rec.metrics.get("path")
        if isinstance(p, str) and p in by_path:
            iter_count = int(rec.metrics.get("iterations", 0))
            by_path[p] = max(by_path[p], iter_count)
    return [by_path[p] for p in paths]


def _terminal_decisions(
    gate_results: list[GateResult],
    paths: list[str],
) -> list[GateDecision]:
    """Extract the terminal (last) gate decision for each output path.

    The write-verify loop may produce multiple intermediate FAIL results
    before reaching a terminal PASS or REVIEW.  For final-status
    determination we only care about the last decision per path — the one
    that actually ended the loop.

    Path accounting:
      - A path whose writer produced content contributes >=1 gate result.
      - A path whose writer returned `has_content=False` breaks the loop
        BEFORE any gate evaluation, so it contributes ZERO gate results.
        For status-determination purposes that is a terminal FAIL (the
        path produced nothing publishable).

    Partitioning is by iteration-boundary (iteration resets to 1 per path);
    sparse groups are padded with FAIL so `len(return) == len(paths)`
    always holds.
    """
    n_paths = len(paths)

    if not gate_results:
        # Every path silently produced no gate result — treat each as FAIL
        # so callers doing `all(d == PASS)` see the right answer (review F-2).
        return [GateDecision.FAIL] * n_paths

    if n_paths <= 1:
        return [gate_results[-1].decision]

    # Multiple paths: partition by iteration==1 boundaries.
    groups: list[list[GateResult]] = []
    current_group: list[GateResult] = []
    for gr in gate_results:
        if gr.iteration == 1 and current_group:
            groups.append(current_group)
            current_group = []
        current_group.append(gr)
    if current_group:
        groups.append(current_group)

    decisions = [group[-1].decision for group in groups]
    # Pad any missing-path groups with FAIL. An empty-content path drops out
    # of the loop before contributing to gate_results, so `len(groups)` can
    # legitimately be less than `n_paths` (review F-2).
    if len(decisions) < n_paths:
        decisions.extend([GateDecision.FAIL] * (n_paths - len(decisions)))
    return decisions


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
        scorer: Scorer | None = None,
    ) -> None:
        self._config = config
        self._taxonomy_plugin = taxonomy_plugin
        self._path_configs = path_configs or {}
        self._evidence_store = evidence_store
        self._discoverer = Discoverer(
            adapter=crawl_adapter,
            config=config.crawl,
            embedding_provider=embedding_provider,
            embedding_batch_size=config.embedding.batch_size,
            embedding_concurrency=config.embedding.concurrency,
            evidence_store=evidence_store,
        )
        self._writer = Writer(llm=llm)
        self._verifier = Verifier(llm=llm)
        # Humanization components (M02+). All optional — None = disabled.
        self._scorer = scorer

    # DEFERRED (audit M1): extract _run_discovery / _run_tag /
    # _run_write_verify_paths / _build_package helpers when a non-cosmetic
    # change requires re-reading run(). Current structure is linear and
    # readable; no refactor pays for itself today.
    # See docs/decompose/audit-2026-04-14/audit-2026-04-14.md §M1.
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

        # Token accumulator — parent dict; each path task gets its own local
        # dict that is merged back after gather (see _run_all_paths).
        token_usage = _zero_tokens()

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

            # Build the evidence-id lookup once and pass it through (audit P8).
            # Writer + _write_verify_loop previously rebuilt this dict on every
            # iteration of every path — O(paths × iterations) sweeps over the
            # same list.
            ev_lookup = {ev.id: ev for ev in evidence}

            # --- Stage 3: Write + Verify loop (per output path, run concurrently) ---
            total_paths = len(request.paths)
            job = self._update_job(
                job,
                JobStatus.RUNNING,
                JobStage.WRITE,
                progress=JobProgress(completed=0, total=total_paths),
            )
            job_logger.info(
                "Starting write-verify across %d path(s) concurrently", total_paths
            )

            all_units, all_gate_results = await self._run_all_paths(
                request=request,
                evidence=evidence,
                gate=gate,
                lineage=lineage,
                job=job,
                job_logger=job_logger,
                token_usage=token_usage,
                ev_lookup=ev_lookup,
            )

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

            per_path_iterations = _per_path_iteration_counts(job, request.paths)
            job_logger.info(
                _format_completion_line(
                    token_usage=token_usage,
                    path_count=len(request.paths),
                    per_path_iterations=per_path_iterations,
                )
            )

            return PipelineResult(
                package=package, job=job, gate_results=all_gate_results
            )

        except Exception as e:
            # TaskGroup wraps failures in ExceptionGroup; unwrap to the first
            # underlying exception so job.error.message stays readable.
            inner = e
            if isinstance(e, BaseExceptionGroup) and e.exceptions:
                inner = e.exceptions[0]
            job_logger.exception("Pipeline run %s failed: %s", run_id, inner)
            return PipelineResult(
                package=None,
                job=self._update_job(job, JobStatus.FAILED, error_msg=str(inner)),
                gate_results=[],
            )

    async def _run_one_path(
        self,
        *,
        request: CurationRequest,
        evidence: list[Evidence],
        path: str,
        gate: QualityGate,
        lineage: ContentLineage,
        job: Job,
        parent_logger: logging.Logger | logging.LoggerAdapter,
        ev_lookup: dict[str, Evidence],
    ) -> tuple[ContentUnit | None, list[GateResult], dict[str, int]]:
        """Run one path's writer/verifier loop with a local token dict and child logger."""
        # LoggerAdapter doesn't expose .getChild on every Python version — reach
        # through to the underlying logger, then re-wrap to preserve any `extra`.
        if isinstance(parent_logger, logging.LoggerAdapter):
            base_child = parent_logger.logger.getChild(path)
            path_logger: logging.Logger | logging.LoggerAdapter = logging.LoggerAdapter(
                base_child, extra=dict(parent_logger.extra or {})
            )
        else:
            path_logger = parent_logger.getChild(path)

        path_tokens = _zero_tokens()
        unit, gate_results = await self._write_verify_loop(
            request=request,
            evidence=evidence,
            path=path,
            gate=gate,
            lineage=lineage,
            job=job,
            job_logger=path_logger,
            token_usage=path_tokens,
            ev_lookup=ev_lookup,
        )
        return unit, gate_results, path_tokens

    async def _run_all_paths(
        self,
        *,
        request: CurationRequest,
        evidence: list[Evidence],
        gate: QualityGate,
        lineage: ContentLineage,
        job: Job,
        job_logger: logging.Logger | logging.LoggerAdapter,
        token_usage: dict[str, int],
        ev_lookup: dict[str, Evidence],
    ) -> tuple[list[ContentUnit], list[GateResult]]:
        """Fan per-path write-verify loops out concurrently (audit P1).

        Per-task token dicts are merged back into `token_usage` after gather.
        A lock-protected completion counter drives in-progress job updates so
        callers watching `job.progress` still see monotonically increasing
        completion numbers even though paths finish out of submission order.
        """
        total = len(request.paths)
        completed = 0
        counter_lock = asyncio.Lock()

        async def _wrapped(
            path: str,
        ) -> tuple[ContentUnit | None, list[GateResult], dict[str, int], str]:
            nonlocal completed
            unit, gate_results, path_tokens = await self._run_one_path(
                request=request,
                evidence=evidence,
                path=path,
                gate=gate,
                lineage=lineage,
                job=job,
                parent_logger=job_logger,
                ev_lookup=ev_lookup,
            )
            async with counter_lock:
                completed += 1
                job_logger.info(
                    "Progress: %d/%d paths complete (finished '%s')",
                    completed,
                    total,
                    path,
                )
                self._update_job(
                    job,
                    JobStatus.RUNNING,
                    JobStage.WRITE,
                    progress=JobProgress(completed=completed, total=total),
                )
            return unit, gate_results, path_tokens, path

        # Preserve submission order in the output so downstream ordering
        # (e.g. _terminal_decisions) remains stable across runs.
        # TaskGroup cancels all sibling tasks atomically on the first exception,
        # so no abandoned path task can mutate `job` after the outer try/except
        # has marked it FAILED (review finding C1 from M01-M05 review).
        async with asyncio.TaskGroup() as tg:
            path_tasks = [tg.create_task(_wrapped(p)) for p in request.paths]
        results = [t.result() for t in path_tasks]

        all_units: list[ContentUnit] = []
        all_gate_results: list[GateResult] = []
        for unit, gate_results, path_tokens, _path in results:
            all_gate_results.extend(gate_results)
            if unit is not None:
                all_units.append(unit)
            _merge_tokens(token_usage, path_tokens)

        return all_units, all_gate_results

    async def _write_verify_loop(
        self,
        request: CurationRequest,
        evidence: list[Evidence],
        path: str,
        gate: QualityGate,
        lineage: ContentLineage,
        job: Job | None = None,
        job_logger: logging.Logger | logging.LoggerAdapter | None = None,
        token_usage: dict | None = None,
        ev_lookup: dict[str, Evidence] | None = None,
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
        # ev_lookup is built once in Pipeline.run() and passed down (audit P8).
        # Falls back to a local rebuild when called directly (e.g. unit tests).
        if ev_lookup is None:
            ev_lookup = {ev.id: ev for ev in evidence}

        # Per-path evidence cap (keeps full list for tag aggregation)
        path_evidence = evidence
        if path_config and path_config.max_evidence:
            path_evidence = evidence[: path_config.max_evidence]

        # Pre-format the evidence prompt blocks once per path — all iterations
        # share the same blocks since `path_evidence` is immutable here
        # (audit P7). Saves (iterations - 1) formatting passes per path.
        writer_block = format_evidence_for_prompt(path_evidence, style="writer")
        verifier_block = format_evidence_for_prompt(path_evidence, style="verifier")

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
                evidence_block=writer_block,
                ev_lookup=ev_lookup,
            )

            # Accumulate token usage from writer
            if _tokens and writer_output.token_usage:
                for key in _tokens:
                    _tokens[key] += writer_output.token_usage.get(key, 0)

            if not writer_output.has_content:
                _log.warning("Writer produced no content for path '%s'", path)
                break

            # `has_content=True` implies the writer produced a non-None unit
            # with non-empty content. Narrow the type for pyright.
            assert writer_output.unit is not None
            unit = writer_output.unit

            if job is not None:
                write_tokens = writer_output.token_usage or {}
                job.stages.append(
                    StageRecord(
                        stage=JobStage.WRITE,
                        started_at=write_start,
                        completed_at=datetime.now(UTC),
                        metrics={
                            "path": path,
                            "iterations": iteration,
                            "tokens_input": write_tokens.get("input_tokens", 0),
                            "tokens_output": write_tokens.get("output_tokens", 0),
                            "tokens_cache_read": write_tokens.get(
                                "cache_read_input_tokens", 0
                            ),
                            "tokens_cache_write": write_tokens.get(
                                "cache_creation_input_tokens", 0
                            ),
                        },
                    )
                )

            # Programmatic style scoring (humanization M02). Soft signal in
            # v1 — the gate still evaluates `unit.scores` (ContentScores)
            # only; style scores inform M03 editor invocation and feed into
            # StageRecord.metrics for threshold calibration. See ADR-002/004/006.
            if self._scorer is not None:
                score_start = datetime.now(UTC)
                style_scores = self._scorer.score(unit.content)
                unit = unit.model_copy(update={"style_scores": style_scores})
                if job is not None:
                    job.stages.append(
                        StageRecord(
                            stage=JobStage.SCORE,
                            started_at=score_start,
                            completed_at=datetime.now(UTC),
                            metrics={
                                "path": path,
                                "sentence_length_stddev": style_scores.sentence_length_stddev,
                                "suppressed_vocab_hits": style_scores.suppressed_vocab_hits,
                                "type_token_ratio": style_scores.type_token_ratio,
                                "formulaic_transition_count": style_scores.formulaic_transition_count,
                                "contrastive_frame_count": style_scores.contrastive_frame_count,
                                "hedging_phrase_count": style_scores.hedging_phrase_count,
                                "word_count": style_scores.word_count,
                                "humanization_pass": style_scores.humanization_pass,
                            },
                        )
                    )

            # Verify
            verify_start = datetime.now(UTC)
            jurisdiction = (
                request.constraints.jurisdiction if request.constraints else None
            )
            report = await self._verifier.verify(
                unit,
                path_evidence,
                jurisdiction=jurisdiction,
                evidence_block=verifier_block,
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
                        metrics={
                            "path": path,
                            "total_claims": report.total_claims,
                            "supported": report.supported,
                            "pass_rate": report.pass_rate,
                            "confidence_score": report.confidence_score,
                        },
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

            # Update unit scores from verification. `style_scores` carries
            # through from the humanization M02 scoring step above.
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
                style_scores=unit.style_scores,
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
