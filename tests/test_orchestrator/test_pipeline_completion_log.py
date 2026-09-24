"""Tests for the pipeline completion summary line (audit D3, T-04.04)."""

from __future__ import annotations

import logging

import pytest

from cce.models.job import Job, JobStage, StageRecord
from cce.orchestrator.pipeline import (
    Pipeline,
    _format_completion_line,
    _per_path_iteration_counts,
)
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import make_adapter
from tests.test_orchestrator.test_pipeline_sequential_paths import _RecordingLLM

# ---------------------------------------------------------------------------
# Unit-level: formatter + per-path iteration extractor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_completion_line_no_cost_estimate():
    line = _format_completion_line(
        token_usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        },
        path_count=2,
        per_path_iterations=[2, 3],
    )
    assert line == (
        "Pipeline complete: input=10, (cache_read=2, cache_write=1), "
        "output=5, paths=2, iterations=[2, 3]"
    )
    assert "est_cost" not in line


@pytest.mark.unit
def test_completion_line_with_cost_estimate():
    line = _format_completion_line(
        token_usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        },
        path_count=2,
        per_path_iterations=[2, 3],
        cost_estimate_usd=0.18,
    )
    assert "est_cost=$0.1800" in line
    assert line.startswith("Pipeline complete: input=10")


@pytest.mark.unit
def test_completion_line_thousands_separator():
    """Large token counts format with commas for readability."""
    line = _format_completion_line(
        token_usage={
            "input_tokens": 12453,
            "output_tokens": 2891,
            "cache_read_input_tokens": 8901,
            "cache_creation_input_tokens": 3200,
        },
        path_count=3,
        per_path_iterations=[2, 3, 2],
    )
    assert "input=12,453" in line
    assert "cache_read=8,901" in line
    assert "cache_write=3,200" in line
    assert "output=2,891" in line


@pytest.mark.unit
def test_per_path_iteration_counts_orders_by_submission():
    """Output follows the order of `paths`, NOT the order records were appended."""
    job = Job(id="j1", request=make_curation_request(paths=["a", "b", "c"]))
    # Append out-of-order to simulate parallel completion.
    from datetime import UTC, datetime

    def _rec(path: str, iteration: int) -> StageRecord:
        return StageRecord(
            stage=JobStage.WRITE,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            metrics={"path": path, "iterations": iteration},
        )

    job.stages.extend(
        [
            _rec("b", 1),
            _rec("b", 2),
            _rec("b", 3),  # path 'b' reached iteration 3
            _rec("a", 1),
            _rec("a", 2),  # path 'a' reached iteration 2
            _rec("c", 1),
            _rec("c", 2),  # path 'c' reached iteration 2
        ]
    )

    assert _per_path_iteration_counts(job, ["a", "b", "c"]) == [2, 3, 2]


@pytest.mark.unit
def test_per_path_iteration_counts_ignores_non_write_stages():
    """DISCOVER / VERIFY / PUBLISH records don't contribute to iteration count."""
    from datetime import UTC, datetime

    job = Job(id="j1", request=make_curation_request(paths=["a"]))
    job.stages.extend(
        [
            StageRecord(
                stage=JobStage.VERIFY,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                metrics={
                    "path": "a",
                    "total_claims": 5,
                    "supported": 5,
                    "pass_rate": 1.0,
                    "confidence_score": 1.0,
                },
            ),
            StageRecord(
                stage=JobStage.WRITE,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                metrics={"path": "a", "iterations": 1},
            ),
        ]
    )
    assert _per_path_iteration_counts(job, ["a"]) == [1]


# ---------------------------------------------------------------------------
# Integration: completion line emitted exactly once per pipeline run
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_completion_line_emitted_once(sqlite_store, caplog):
    config = make_engine_config()
    pipeline = Pipeline(
        config=config,
        crawl_adapter=make_adapter(),
        evidence_store=sqlite_store,
        llm=_RecordingLLM(),
    )

    with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
        result = await pipeline.run(
            make_curation_request(paths=["blog", "summary", "faq"]),
            make_source_policy(),
        )
    assert result.succeeded is True

    completion_lines = [
        r for r in caplog.records if "Pipeline complete" in r.getMessage()
    ]
    assert len(completion_lines) == 1
    msg = completion_lines[0].getMessage()
    # 3 paths, each PASS on iteration 1 with the sleepy stub.
    assert "paths=3" in msg
    assert "iterations=[1, 1, 1]" in msg
