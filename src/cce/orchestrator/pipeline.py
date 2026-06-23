"""Pipeline orchestrator.

Wires the core loop: discover -> store -> write -> verify -> gate -> (loop or publish).
Phase 1 entry point. Single-threaded at the process level; the per-path
writer/verifier loops run sequentially in path order (learn -> explore -> apply)
so each path receives a digest of its already-written siblings and builds on
rather than re-explains them (M03, ADR-003).
"""

from __future__ import annotations

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
from cce.models.evidence import DiscoveryResult, Evidence
from cce.models.job import Job, JobError, JobProgress, JobStage, JobStatus, StageRecord
from cce.models.package import PackageLineage, PublishPackage
from cce.models.paths import PathConfig
from cce.models.request import CurationRequest
from cce.models.style import StyleScores
from cce.policy.types import SourcePolicy
from cce.synthesis.editor import Editor
from cce.synthesis.implied_claims import ImpliedClaimAnnotation, ImpliedClaimChecker
from cce.synthesis.scoring import Scorer
from cce.synthesis.writer import Writer, WriterOutput
from cce.tagging.base import TaxonomyPlugin, TaxonomyUnavailableError
from cce.verification.gate import GateDecision, GateResult, QualityGate
from cce.verification.verifier import VerificationReport, Verifier

logger = logging.getLogger(__name__)


# --- Token-usage helpers (audit P1) ---------------------------------------
# Each per-path loop owns its own dict; the parent merges it back into the
# job-level accumulator after the path finishes (sequential since M03).

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
    """Max WRITE iteration number reached per path, in `paths` order.

    Groups by the explicit (path, iteration) keys on each record —
    ``StageRecord.path`` with a ``metrics["path"]`` fallback for records
    written before the field existed (T-07.05) — so the output is invariant
    to the order records were appended.
    """
    by_path: dict[str, int] = dict.fromkeys(paths, 0)
    for rec in job.stages:
        if rec.stage != JobStage.WRITE or not rec.metrics:
            continue
        p = rec.path if rec.path is not None else rec.metrics.get("path")
        if isinstance(p, str) and p in by_path:
            iter_count = int(rec.metrics.get("iterations", 0))
            by_path[p] = max(by_path[p], iter_count)
    return [by_path[p] for p in paths]


def _terminal_decisions(
    gate_results: list[GateResult],
    paths: list[str],
    by_path: Mapping[str, list[GateResult]] | None = None,
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

    When ``by_path`` is provided (the pipeline call site — T-07.05), grouping
    uses the explicit (path, iteration) keys: per path, the gate result with
    the highest iteration is terminal, independent of list order. Without
    ``by_path`` (direct callers that only have the flat stream), the original
    partitioning by iteration-boundary (iteration resets to 1 per path)
    applies; sparse groups are padded with FAIL so `len(return) == len(paths)`
    always holds.
    """
    n_paths = len(paths)

    if by_path is not None:
        decisions: list[GateDecision] = []
        for path in paths:
            group = by_path.get(path) or []
            if group:
                decisions.append(max(group, key=lambda gr: gr.iteration).decision)
            else:
                # Empty-content path: dropped out of the loop before any gate
                # evaluation — terminal FAIL (review F-2).
                decisions.append(GateDecision.FAIL)
        return decisions

    # Flat-stream fallback (``by_path is None``): no longer reached from the
    # pipeline since M03 — _run_all_paths always builds and passes
    # gate_results_by_path — but kept for direct/test callers that only hold the
    # flat result stream.
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


def _build_sibling_digest(units: list[ContentUnit]) -> str:
    """Compact digest of already-written sibling drafts, fed to the next path's writer.

    Uses the claim text from each unit's evidence_map (already a structured list
    of the claims the writer made) so the next path sees WHAT was covered without
    re-ingesting full prose. Kept short — at most 25 claims per sibling — to bound
    the added input-token cost (Stage 1 security plan, ADR-003).

    Prose-level de-duplication only: the digest tells the next writer what NOT to
    re-explain; it never touches citations or the evidence pool, so later paths
    remain free to cite the same sources.
    """
    parts: list[str] = []
    for u in units:
        claims = [m.claim for m in u.evidence_map][:25]
        if not claims:
            # Fallback for the writer's degraded (non-JSON) parse path, which
            # yields evidence_map=[]: use the draft's own section headings so a
            # degraded sibling still signals what it covered.
            claims = [
                ln.lstrip("#").strip()
                for ln in u.content.splitlines()
                if ln.strip().startswith("#")
            ][:25]
        parts.append(
            f"## From the '{u.path}' article:\n" + "\n".join(f"- {c}" for c in claims)
        )
    return "\n\n".join(parts)


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
        editor: Editor | None = None,
        implied_claim_checker: ImpliedClaimChecker | None = None,
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
        self._editor = editor
        self._implied_claim_checker = implied_claim_checker

    async def run(
        self,
        request: CurationRequest,
        policy: SourcePolicy,
    ) -> PipelineResult:
        """Execute the full pipeline for a curation request.

        Returns a PipelineResult containing the PublishPackage (if successful),
        the Job tracking object, and any gate results from the verification loop.

        Orchestration narrative only — stage bodies live in the phase helpers
        (M07, lifted verbatim per ADR-005; closes audit M1).
        """
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        job = Job(id=f"job_{uuid.uuid4().hex[:12]}", request=request)

        # Job-scoped logger — all pipeline logs include job_id
        job_logger = logging.LoggerAdapter(logger, extra={"job_id": job.id})
        job_logger.info("Pipeline run %s started for topic '%s'", run_id, request.topic)

        # Token accumulator — parent dict; each path gets its own local dict
        # that is merged back as the path finishes (see _run_all_paths).
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
            discovery = await self._run_discovery(request, policy, job, job_logger)
            evidence = discovery.evidence

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
                evidence = await self._run_tagging(evidence, job, job_logger)

            # --- Stage 2: Store evidence ---
            ev_lookup = await self._store_evidence(evidence, job_logger)

            # --- Stage 3: Write + Verify loop (per output path, run sequentially) ---
            (
                all_units,
                all_gate_results,
                gate_results_by_path,
            ) = await self._run_all_paths(
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
            final_status = self._interpret_terminal_decisions(
                all_gate_results, request.paths, gate_results_by_path
            )
            package = self._build_output_package(
                job=job,
                request=request,
                run_id=run_id,
                units=all_units,
                evidence=evidence,
                token_usage=token_usage,
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
            # Paths now run sequentially (M03), so a failing path raises its
            # exception directly — no grouped-exception unwrap needed.
            job_logger.exception("Pipeline run %s failed: %s", run_id, e)
            return PipelineResult(
                package=None,
                job=self._update_job(job, JobStatus.FAILED, error_msg=str(e)),
                gate_results=[],
            )

    # --- run() phase helpers (M07 — extracted from run(), bodies lifted
    # verbatim per ADR-005; finding 1.2) --------------------------------------

    async def _run_discovery(
        self,
        request: CurationRequest,
        policy: SourcePolicy,
        job: Job,
        job_logger: logging.Logger | logging.LoggerAdapter,
    ) -> DiscoveryResult:
        """Run the discovery stage and append its StageRecord."""
        job = self._update_job(job, JobStatus.RUNNING, JobStage.DISCOVER)
        stage_start = datetime.now(UTC)

        discovery = await self._discoverer.discover(request, policy)
        job_logger.info("Discovered %d evidence objects", len(discovery.evidence))

        job.stages.append(
            StageRecord(
                stage=JobStage.DISCOVER,
                started_at=stage_start,
                completed_at=datetime.now(UTC),
                metrics=discovery.metrics or None,
            )
        )
        return discovery

    async def _run_tagging(
        self,
        evidence: list[Evidence],
        job: Job,
        job_logger: logging.Logger | logging.LoggerAdapter,
    ) -> list[Evidence]:
        """Tag evidence via the taxonomy plugin and append the TAG StageRecord.

        Caller guards on ``self._taxonomy_plugin is not None``.
        """
        assert self._taxonomy_plugin is not None
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
            job_logger.info("Tagged %d evidence objects with taxonomy", len(evidence))
        except TaxonomyUnavailableError:
            job_logger.warning("Taxonomy plugin unavailable, proceeding without tags")
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
        return evidence

    async def _store_evidence(
        self,
        evidence: list[Evidence],
        job_logger: logging.Logger | logging.LoggerAdapter,
    ) -> dict[str, Evidence]:
        """Persist evidence and return the evidence-id lookup."""
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
        return {ev.id: ev for ev in evidence}

    def _interpret_terminal_decisions(
        self,
        gate_results: list[GateResult],
        paths: list[str],
        gate_results_by_path: Mapping[str, list[GateResult]] | None = None,
    ) -> JobStatus:
        """Map terminal gate decisions to the job's final status.

        Determines final status based on the *terminal* gate decision
        for each output path.  Intermediate FAIL decisions (which
        triggered rewrites) are not terminal — only the last result
        per path matters.
        """
        final_decisions = _terminal_decisions(
            gate_results, paths, by_path=gate_results_by_path
        )

        if all(d == GateDecision.PASS for d in final_decisions):
            return JobStatus.COMPLETED
        elif any(d == GateDecision.REVIEW for d in final_decisions):
            return JobStatus.REVIEW_REQUIRED
        else:
            # Gate returned FAIL after max iterations — content needs human review
            return JobStatus.REVIEW_REQUIRED

    def _build_output_package(
        self,
        *,
        job: Job,
        request: CurationRequest,
        run_id: str,
        units: list[ContentUnit],
        evidence: list[Evidence],
        token_usage: dict[str, int],
    ) -> PublishPackage:
        """Assemble the publish package and append the PUBLISH StageRecord."""
        job = self._update_job(job, JobStatus.RUNNING, JobStage.PUBLISH)
        stage_start = datetime.now(UTC)

        # Aggregate scores
        if units:
            avg_confidence = sum(u.scores.confidence for u in units) / len(units)
            avg_coverage = sum(u.scores.coverage for u in units) / len(units)
            avg_diversity = sum(u.scores.source_diversity for u in units) / len(units)
        else:
            avg_confidence = avg_coverage = avg_diversity = 0.0

        package = PublishPackage(
            job_id=job.id,
            units=units,
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
        return package

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
        job_token_usage: Mapping[str, int] | None = None,
        sibling_context: str | None = None,
    ) -> tuple[ContentUnit | None, list[GateResult], dict[str, int]]:
        """Run one path's writer/verifier loop with a local token dict and child logger.

        ``sibling_context`` (M03) is threaded straight through to the writer so
        this path builds on, rather than re-explains, the paths written before it.
        """
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
            job_token_usage=job_token_usage,
            sibling_context=sibling_context,
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
    ) -> tuple[list[ContentUnit], list[GateResult], dict[str, list[GateResult]]]:
        """Run per-path write-verify loops SEQUENTIALLY so each path sees its
        siblings (M03, ADR-003).

        Replaces the prior parallel per-path fan-out (audit P1) with a serial
        loop over ``request.paths`` (learn -> explore -> apply) to enable
        cross-path de-duplication. Before each path a digest of written siblings
        is built (``_build_sibling_digest``) and threaded into the writer prompt;
        de-duplication is prose-level only, so citations and the shared evidence
        pool are untouched.

        Gate attribution is unaffected: results are still grouped explicitly in
        ``gate_results_by_path``, which feeds the (path, iteration) terminal-
        decision grouping in ``_interpret_terminal_decisions`` (T-07.05). Each
        path's local token dict is merged into ``token_usage`` as it finishes, so
        a later path's budget checkpoint sees every prior path's spend.

        Returns ``(units, flat gate results, gate results grouped by path)``.
        """
        total = len(request.paths)
        job = self._update_job(
            job,
            JobStatus.RUNNING,
            JobStage.WRITE,
            progress=JobProgress(completed=0, total=total),
        )
        job_logger.info("Starting write-verify across %d path(s) sequentially", total)

        all_units: list[ContentUnit] = []
        all_gate_results: list[GateResult] = []
        gate_results_by_path: dict[str, list[GateResult]] = {}

        for completed, path in enumerate(request.paths, start=1):
            # Build the sibling digest from prior paths' drafts (None for the
            # first path) and thread it into this path's writer prompt.
            sibling_context = _build_sibling_digest(all_units) if all_units else None
            unit, gate_results, path_tokens = await self._run_one_path(
                request=request,
                evidence=evidence,
                path=path,
                gate=gate,
                lineage=lineage,
                job=job,
                parent_logger=job_logger,
                ev_lookup=ev_lookup,
                job_token_usage=token_usage,
                sibling_context=sibling_context,
            )
            all_gate_results.extend(gate_results)
            gate_results_by_path[path] = gate_results
            if unit is not None:
                all_units.append(unit)
            _merge_tokens(token_usage, path_tokens)
            job_logger.info(
                "Progress: %d/%d paths complete (finished '%s')", completed, total, path
            )
            self._update_job(
                job,
                JobStatus.RUNNING,
                JobStage.WRITE,
                progress=JobProgress(completed=completed, total=total),
            )

        return all_units, all_gate_results, gate_results_by_path

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
        job_token_usage: Mapping[str, int] | None = None,
        sibling_context: str | None = None,
    ) -> tuple[ContentUnit | None, list[GateResult]]:
        """Run the writer-verifier loop for a single output path.

        Scoring, editing, and verification live in the phase helpers below
        (M07, bodies lifted verbatim — ADR-005); the writer invocation and
        gate evaluation stay inline: they are the loop.

        Token budget (M08, ADR-003): when ``max_tokens_per_job`` is set, a
        checkpoint at the top of each writer iteration compares the job-level
        accumulated usage against the budget. ``job_token_usage`` is the
        shared job dict (the baseline of all previously-completed paths, merged
        after each path); see the checkpoint comment for the read pattern.
        Checkpoint granularity is one iteration, so the worst-case overshoot is
        one writer+verifier call pair (paths run sequentially since M03).
        """
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
            # --- Budget checkpoint (M08, ADR-003) ---
            budget = self._config.max_tokens_per_job
            if budget is not None:
                # Job-level accumulated usage = the shared job dict plus this
                # path's local dict. Paths run sequentially (M03), so
                # `job_token_usage` already holds every previously-completed
                # path's spend (merged after each path in _run_all_paths) and
                # there are no in-flight siblings — the checkpoint sees the full
                # prior spend plus this path's tokens so far.
                spent_view: dict[str, int] = dict(job_token_usage or {})
                for key, value in (_tokens or {}).items():
                    spent_view[key] = spent_view.get(key, 0) + int(value)
                if self._budget_exceeded(spent_view, budget):
                    spent = spent_view.get("input_tokens", 0) + spent_view.get(
                        "output_tokens", 0
                    )
                    _log.warning(
                        "Path '%s': token budget exceeded — spent %d of %d "
                        "before iteration %d; stopping and routing to review "
                        "(ADR-003)",
                        path,
                        spent,
                        budget,
                        iteration,
                    )
                    if gate_results:
                        # A budget-stopped path carries FAIL/REVIEW from its
                        # last real gate evaluation (a PASS returns out of the
                        # loop above), so _interpret_terminal_decisions already
                        # routes the job to REVIEW_REQUIRED — assert the
                        # invariant instead of re-deriving a decision here.
                        assert gate_results[-1].decision != GateDecision.PASS
                        gate_results[-1].feedback += (
                            f"\nToken budget exceeded: stopped before iteration "
                            f"{iteration} (spent {spent:,} of {budget:,} tokens). "
                            f"Partial draft kept for review (ADR-003)."
                        )
                    if job is not None:
                        checkpoint_at = datetime.now(UTC)
                        job.stages.append(
                            StageRecord(
                                stage=JobStage.WRITE,
                                path=path,
                                started_at=checkpoint_at,
                                completed_at=checkpoint_at,
                                metrics={
                                    "path": path,
                                    "budget_exceeded": True,
                                    "stopped_before_iteration": iteration,
                                    "tokens_spent": spent,
                                    "max_tokens_per_job": budget,
                                },
                            )
                        )
                    break

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
                sibling_context=sibling_context,
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

            self._record_write_stage(job, path, iteration, write_start, writer_output)

            # Score (humanization M02)
            unit, style_scores = self._score_draft(unit, path, job)

            # Conditional stylistic rewrite (humanization M03). Fires only
            # when the scorer flagged the draft. Does NOT consume an iteration
            # slot (ADR-005).
            if (
                self._editor is not None
                and style_scores is not None
                and not style_scores.humanization_pass
            ):
                unit = await self._edit_draft(
                    unit,
                    style_scores=style_scores,
                    path_evidence=path_evidence,
                    path_config=path_config,
                    path=path,
                    iteration=iteration,
                    job=job,
                    token_usage=_tokens,
                    log=_log,
                )

            # Verify
            report = await self._run_verifier(
                unit,
                request=request,
                path_evidence=path_evidence,
                verifier_block=verifier_block,
                path=path,
                job=job,
                token_usage=_tokens,
            )

            unit = self._apply_verification_scores(unit, report, ev_lookup)

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

    # --- _write_verify_loop phase helpers (M07 — bodies lifted verbatim per
    # ADR-005; finding 1.2). Token accumulation threads the per-path dict
    # (audit P1 pattern) through `token_usage`. ------------------------------

    @staticmethod
    def _budget_exceeded(
        token_usage: Mapping[str, int], max_tokens_per_job: int | None
    ) -> bool:
        """True when accumulated input+output tokens meet or exceed the budget.

        Called at the top of each writer iteration (M08, ADR-003). Only
        ``input_tokens + output_tokens`` count toward the budget; cache
        read/write counts are reported separately and stay outside it. Since
        the checkpoint granularity is one iteration, the worst-case overshoot
        is one writer+verifier call pair (paths run sequentially since M03).
        """
        if max_tokens_per_job is None:
            return False
        spent = token_usage.get("input_tokens", 0) + token_usage.get("output_tokens", 0)
        return spent >= max_tokens_per_job

    @staticmethod
    def _record_write_stage(
        job: Job | None,
        path: str,
        iteration: int,
        write_start: datetime,
        writer_output: WriterOutput,
    ) -> None:
        """Append the WRITE StageRecord for one writer invocation."""
        if job is not None:
            write_tokens = writer_output.token_usage or {}
            job.stages.append(
                StageRecord(
                    stage=JobStage.WRITE,
                    path=path,
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

    def _score_draft(
        self, unit: ContentUnit, path: str, job: Job | None
    ) -> tuple[ContentUnit, StyleScores | None]:
        """Programmatic style scoring (humanization M02). Soft signal in
        v1 — the gate still evaluates `unit.scores` (ContentScores)
        only; style scores inform M03 editor invocation and feed into
        StageRecord.metrics for threshold calibration. See ADR-002/004/006.

        No-op when the scorer isn't wired — returns ``(unit, None)``.
        """
        style_scores: StyleScores | None = None
        if self._scorer is not None:
            score_start = datetime.now(UTC)
            style_scores = self._scorer.score(unit.content)
            unit = unit.with_style_scores(style_scores)
            if job is not None:
                job.stages.append(
                    StageRecord(
                        stage=JobStage.SCORE,
                        path=path,
                        started_at=score_start,
                        completed_at=datetime.now(UTC),
                        metrics={
                            "path": path,
                            "sentence_length_stddev": style_scores.sentence_length_stddev,
                            "suppressed_vocab_hits": style_scores.suppressed_vocab_hits,
                            "type_token_ratio": style_scores.type_token_ratio,
                            "formulaic_transition_count": style_scores.formulaic_transition_count,
                            "contrastive_frame_count": style_scores.contrastive_frame_count,
                            "contrastive_parasitic_count": style_scores.contrastive_parasitic_count,
                            "contrastive_alternative_count": style_scores.contrastive_alternative_count,
                            "hedging_phrase_count": style_scores.hedging_phrase_count,
                            "em_dash_count": style_scores.em_dash_count,
                            "word_count": style_scores.word_count,
                            "humanization_pass": style_scores.humanization_pass,
                        },
                    )
                )
        return unit, style_scores

    async def _edit_draft(
        self,
        unit: ContentUnit,
        *,
        style_scores: StyleScores,
        path_evidence: list[Evidence],
        path_config: PathConfig | None,
        path: str,
        iteration: int,
        job: Job | None,
        token_usage: dict | None,
        log: logging.Logger | logging.LoggerAdapter,
    ) -> ContentUnit:
        """Stylistic rewrite via the Editor (humanization M03).

        Preserves citations as a hard constraint; on citation drift the
        writer's original draft is retained so the verifier still runs
        against known-good content — and ``draft_source`` stays "writer"
        (finding 1.5, T-07.05). Caller guards on editor wiring + score fail.
        """
        assert self._editor is not None
        _tokens = token_usage
        # Implied-claim annotations (humanization M04). Runs before
        # the editor so its rewrite hints feed into the editor prompt.
        # Skipped when the checker isn't wired — annotations stay [].
        annotations: list[ImpliedClaimAnnotation] = []
        if self._implied_claim_checker is not None:
            annotations = await self._implied_claim_checker.check(
                unit.content, cited_evidence=path_evidence
            )
            log.info(
                "ImpliedClaimChecker: %d annotation(s) for path '%s' iter %d",
                len(annotations),
                path,
                iteration,
            )

        edit_start = datetime.now(UTC)
        editor_output = await self._editor.edit(
            unit,
            path_config=path_config,
            scores=style_scores,
            # Explicit [] when the checker ran and produced nothing;
            # preserves the distinction from None ("checker not wired")
            # if a future caller ever needs it.
            annotations=[a.rewrite_hint for a in annotations],
        )
        if _tokens and editor_output.token_usage:
            _merge_tokens(_tokens, editor_output.token_usage)
        if editor_output.succeeded:
            # EditorOutput.succeeded implies citations were preserved —
            # record the draft provenance flip (finding 1.5, T-07.05).
            unit = unit.model_copy(
                update={
                    "content": editor_output.edited_content,
                    "draft_source": "editor",
                }
            )
        if job is not None:
            job.stages.append(
                StageRecord(
                    stage=JobStage.EDIT,
                    path=path,
                    started_at=edit_start,
                    completed_at=datetime.now(UTC),
                    metrics={
                        "path": path,
                        "invoked": True,
                        "citations_preserved": editor_output.citations_preserved,
                        "word_count_before": editor_output.word_count_before,
                        "word_count_after": editor_output.word_count_after,
                        "tokens_input": editor_output.token_usage.get(
                            "input_tokens", 0
                        ),
                        "tokens_output": editor_output.token_usage.get(
                            "output_tokens", 0
                        ),
                        "tokens_cache_read": editor_output.token_usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        "tokens_cache_write": editor_output.token_usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                    },
                )
            )
        return unit

    async def _run_verifier(
        self,
        unit: ContentUnit,
        *,
        request: CurationRequest,
        path_evidence: list[Evidence],
        verifier_block: str,
        path: str,
        job: Job | None,
        token_usage: dict | None,
    ) -> VerificationReport:
        """Verify the draft against path evidence; append the VERIFY record."""
        _tokens = token_usage
        verify_start = datetime.now(UTC)
        jurisdiction = request.constraints.jurisdiction if request.constraints else None
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
                    path=path,
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
        return report

    @staticmethod
    def _apply_verification_scores(
        unit: ContentUnit, report: VerificationReport, ev_lookup: dict[str, Evidence]
    ) -> ContentUnit:
        """Fold verification scores + aggregated evidence tags into the unit."""
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

        # Update unit scores from verification. `style_scores` (and the
        # T-07.05 `draft_source` flag) carry through model_copy — the previous
        # 9-field ContentUnit reconstruction would silently reset any field
        # it didn't enumerate (finding 1.5).
        return unit.model_copy(update={"tags": aggregated_tags}).with_scores(
            ContentScores(
                confidence=report.confidence_score,
                coverage=report.pass_rate,
                source_diversity=unit.scores.source_diversity,
            )
        )

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
