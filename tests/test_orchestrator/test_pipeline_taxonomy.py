"""Tests for taxonomy degradation signaling in the pipeline."""

from __future__ import annotations

import logging

import pytest

from cce.models.job import JobStage
from cce.tagging.base import TaxonomyUnavailableError
from tests.conftest import (
    MockCrawlAdapter,
    MockEmbeddingProvider,
    MockLLMProvider,
    make_crawl_result,
    make_engine_config,
    make_source_policy,
)


def _writer_response() -> str:
    import json

    return json.dumps(
        {
            "content": "## Test\n\nA claim [ev_test_001].",
            "citations_used": ["ev_test_001"],
            "evidence_map": [{"claim": "A claim", "evidence_ids": ["ev_test_001"]}],
            "gaps": [],
        }
    )


def _verifier_response() -> str:
    import json

    return json.dumps(
        {
            "claims": [
                {
                    "claim": "A claim",
                    "citation_ids": ["ev_test_001"],
                    "assessment": "supported",
                    "explanation": "OK",
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
            "overall_feedback": "Good.",
        }
    )


def _mock_llm() -> MockLLMProvider:
    from cce.llm.base import LLMResponse

    return MockLLMProvider(
        responses=[
            LLMResponse(content=_writer_response(), model="mock", usage={}, stop_reason="end_turn"),
            LLMResponse(content=_verifier_response(), model="mock", usage={}, stop_reason="end_turn"),
        ]
    )


class MockTaxonomyPlugin:
    """Mock taxonomy plugin for testing."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def tag_many(self, evidence):
        if self._fail:
            raise TaxonomyUnavailableError("Mock taxonomy failure")
        from dataclasses import dataclass, field

        @dataclass
        class TagResult:
            tags: list[str] = field(default_factory=list)
            signals: dict = field(default_factory=dict)

        return [TagResult(tags=["test"], signals={}) for _ in evidence]


async def _run_pipeline(taxonomy_plugin=None, tmp_path=None):
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore
    from cce.models.request import CurationRequest
    from cce.orchestrator.pipeline import Pipeline

    config = make_engine_config()
    crawl = MockCrawlAdapter(
        search_map={"test topic": ["https://example.com/1"]},
        url_map={"https://example.com/1": make_crawl_result(url="https://example.com/1")},
    )
    store_config = EvidenceStoreConfig(backend="sqlite", sqlite_path=tmp_path / "test.db")
    store = SQLiteEvidenceStore(store_config)
    await store.connect()

    pipeline = Pipeline(
        config=config,
        llm=_mock_llm(),
        crawl_adapter=crawl,
        evidence_store=store,
        embedding_provider=MockEmbeddingProvider(),
        taxonomy_plugin=taxonomy_plugin,
    )
    request = CurationRequest(topic="test topic", paths=["blog"], policy_id="test-policy")
    result = await pipeline.run(request, make_source_policy())
    await store.close()
    return result


class TestTaxonomyDegradation:
    async def test_taxonomy_failure_sets_tags_unavailable(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="cce.orchestrator.pipeline"):
            result = await _run_pipeline(
                taxonomy_plugin=MockTaxonomyPlugin(fail=True), tmp_path=tmp_path
            )

        tag_stages = [s for s in result.job.stages if s.stage == JobStage.TAG]
        assert len(tag_stages) == 1
        assert tag_stages[0].metrics is not None
        assert tag_stages[0].metrics["tags_available"] is False
        assert any("conflict detection" in r.message for r in caplog.records)

    async def test_taxonomy_success_sets_tags_available(self, tmp_path):
        result = await _run_pipeline(
            taxonomy_plugin=MockTaxonomyPlugin(fail=False), tmp_path=tmp_path
        )

        tag_stages = [s for s in result.job.stages if s.stage == JobStage.TAG]
        assert len(tag_stages) == 1
        assert tag_stages[0].metrics is not None
        assert tag_stages[0].metrics["tags_available"] is True

    async def test_no_taxonomy_plugin_no_tag_stage(self, tmp_path):
        result = await _run_pipeline(taxonomy_plugin=None, tmp_path=tmp_path)

        tag_stages = [s for s in result.job.stages if s.stage == JobStage.TAG]
        assert len(tag_stages) == 0
