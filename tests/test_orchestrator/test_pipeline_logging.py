"""Tests for job-scoped logging and token tracking in the pipeline."""

from __future__ import annotations

import logging

import pytest

from cce.models.job import JobStage
from tests.conftest import (
    MockCrawlAdapter,
    MockEmbeddingProvider,
    MockLLMProvider,
    make_crawl_result,
    make_engine_config,
    make_source_policy,
)


def _writer_response(citations: str = "ev_test_001") -> str:
    """Build a valid writer JSON response."""
    import json

    return json.dumps(
        {
            "content": f"## Test\n\nA claim [{citations}].",
            "citations_used": [citations],
            "evidence_map": [{"claim": "A claim", "evidence_ids": [citations]}],
            "gaps": [],
        }
    )


def _verifier_response() -> str:
    """Build a valid verifier JSON response."""
    import json

    return json.dumps(
        {
            "claims": [
                {
                    "claim": "A claim",
                    "citation_ids": ["ev_test_001"],
                    "assessment": "supported",
                    "explanation": "Matches evidence.",
                    "suggestion": "",
                }
            ],
            "summary": {
                "total_claims": 1,
                "supported": 1,
                "unsupported": 0,
                "uncited": 0,
                "leakage": 0,
                "conflicts": 0,
                "gaps_acknowledged": 0,
            },
            "contradictions": [],
            "overall_feedback": "All claims supported.",
        }
    )


def _mock_llm_with_usage(
    input_tokens: int = 100, output_tokens: int = 50
) -> MockLLMProvider:
    """Build a MockLLMProvider that returns responses with non-zero usage."""
    from cce.llm.base import LLMResponse

    def make_response():
        return LLMResponse(
            content=_writer_response(),
            model="mock",
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            stop_reason="end_turn",
        )

    def make_verify_response():
        return LLMResponse(
            content=_verifier_response(),
            model="mock",
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            stop_reason="end_turn",
        )

    # Writer call first, then verifier call
    return MockLLMProvider(responses=[make_response, make_verify_response])


@pytest.fixture
def pipeline_deps(tmp_path):
    """Build pipeline dependencies for logging tests."""
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore
    from cce.orchestrator.pipeline import Pipeline

    async def _build(llm=None):
        config = make_engine_config()
        policy = make_source_policy()

        crawl = MockCrawlAdapter(
            search_map={"test topic": ["https://example.com/1"]},
            url_map={
                "https://example.com/1": make_crawl_result(url="https://example.com/1")
            },
        )

        store_config = EvidenceStoreConfig(
            backend="sqlite", sqlite_path=tmp_path / "test.db"
        )
        store = SQLiteEvidenceStore(store_config)
        await store.connect()

        llm = llm or _mock_llm_with_usage()

        pipeline = Pipeline(
            config=config,
            llm=llm,
            crawl_adapter=crawl,
            evidence_store=store,
            embedding_provider=MockEmbeddingProvider(),
        )
        return pipeline, policy, store

    return _build


class TestJobScopedLogging:
    async def test_job_id_in_log_messages(self, pipeline_deps, caplog):
        from cce.models.request import CurationRequest

        build = pipeline_deps
        pipeline, policy, store = await build()

        request = CurationRequest(
            topic="test topic", paths=["blog"], policy_id="test-policy"
        )

        with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
            result = await pipeline.run(request, policy)

        await store.close()

        # Find log records with job_id in extra
        job_id = result.job.id
        records_with_job_id = [
            r for r in caplog.records if getattr(r, "job_id", None) == job_id
        ]
        assert len(records_with_job_id) > 0, "No log records found with job_id"

    async def test_token_usage_logged_at_completion(self, pipeline_deps, caplog):
        from cce.models.request import CurationRequest

        build = pipeline_deps
        pipeline, policy, store = await build(
            llm=_mock_llm_with_usage(input_tokens=500, output_tokens=200)
        )

        request = CurationRequest(
            topic="test topic", paths=["blog"], policy_id="test-policy"
        )

        with caplog.at_level(logging.INFO, logger="cce.orchestrator.pipeline"):
            await pipeline.run(request, policy)

        await store.close()

        # Check that token usage appears in the structured completion log
        # (new format introduced by audit T-04.04).
        completion_msgs = [
            r.getMessage()
            for r in caplog.records
            if "Pipeline complete" in r.getMessage()
        ]
        assert len(completion_msgs) == 1
        msg = completion_msgs[0]
        assert "input=" in msg
        assert "output=" in msg
        assert "cache_read=" in msg
        assert "cache_write=" in msg
        assert "paths=" in msg
        assert "iterations=" in msg

    async def test_token_usage_in_stage_metrics(self, pipeline_deps):
        from cce.models.request import CurationRequest

        build = pipeline_deps
        pipeline, policy, store = await build(
            llm=_mock_llm_with_usage(input_tokens=300, output_tokens=150)
        )

        request = CurationRequest(
            topic="test topic", paths=["blog"], policy_id="test-policy"
        )

        result = await pipeline.run(request, policy)
        await store.close()

        # Find the PUBLISH stage record (where we store token totals)
        publish_stages = [s for s in result.job.stages if s.stage == JobStage.PUBLISH]
        assert len(publish_stages) == 1
        metrics = publish_stages[0].metrics
        assert metrics is not None
        assert "token_usage" in metrics
        assert metrics["token_usage"]["input_tokens"] > 0
        assert metrics["token_usage"]["output_tokens"] > 0

    async def test_zero_usage_responses_handled(self, pipeline_deps):
        from cce.llm.base import LLMResponse
        from cce.models.request import CurationRequest

        # LLM responses with empty usage dicts
        llm = MockLLMProvider(
            responses=[
                LLMResponse(
                    content=_writer_response(),
                    model="mock",
                    usage={},
                    stop_reason="end_turn",
                ),
                LLMResponse(
                    content=_verifier_response(),
                    model="mock",
                    usage={},
                    stop_reason="end_turn",
                ),
            ]
        )

        build = pipeline_deps
        pipeline, policy, store = await build(llm=llm)

        request = CurationRequest(
            topic="test topic", paths=["blog"], policy_id="test-policy"
        )

        result = await pipeline.run(request, policy)
        await store.close()

        # Should complete without errors even with empty usage
        assert result.job.status.value in ("completed", "review_required")
