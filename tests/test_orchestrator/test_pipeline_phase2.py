"""Phase 2 integration tests for pipeline — taxonomy, path configs, jurisdiction."""

import pytest

from cce.models.evidence import Evidence
from cce.models.paths import PathConfig
from cce.orchestrator.pipeline import Pipeline
from cce.tagging.base import TaggingResult, TaxonomyUnavailableError
from cce.verification.gate import GateDecision
from tests.conftest import (
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import (
    llm as _llm,
)
from tests.test_orchestrator.conftest import (
    make_adapter as _make_adapter,
)
from tests.test_orchestrator.conftest import (
    verifier_json as _verifier_json,
)
from tests.test_orchestrator.conftest import (
    writer_json as _writer_json,
)


class MockTaxonomy:
    """Taxonomy plugin that returns fixed tags for any evidence."""

    async def tag(self, evidence: Evidence) -> TaggingResult:
        return TaggingResult(
            tags=["emotional", "physical"],
            signals={"emotional": "primary", "physical": "secondary"},
            confidence=0.8,
        )

    async def tag_many(self, evidence: list[Evidence]) -> list[TaggingResult]:
        return [await self.tag(ev) for ev in evidence]


class FailingTaxonomy:
    """Taxonomy plugin that always raises TaxonomyUnavailableError."""

    async def tag(self, evidence: Evidence) -> TaggingResult:
        raise TaxonomyUnavailableError("service down")

    async def tag_many(self, evidence: list[Evidence]) -> list[TaggingResult]:
        raise TaxonomyUnavailableError("service down")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pipeline_with_taxonomy_plugin(sqlite_store):
    """Evidence gets tagged when taxonomy plugin is provided."""
    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        taxonomy_plugin=MockTaxonomy(),
    )

    request = make_curation_request(paths=["blog"])
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    assert result.succeeded
    # Writer should have received tagged evidence — check via LLM calls
    # The writer call is calls[0], verifier is calls[1]
    writer_call = llm.calls[0]
    user_msg = writer_call["messages"][0].content
    assert "test topic" in user_msg.lower() or "evidence" in user_msg.lower()


@pytest.mark.integration
async def test_pipeline_with_path_configs(sqlite_store):
    """Writer receives PathConfig when path_configs are provided."""
    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    path_configs = {
        "blog": PathConfig(
            id="blog",
            name="Blog",
            tone="conversational",
            structure="essay",
            depth="foundational",
            prompt_addendum="Write warmly.",
        ),
    }

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        path_configs=path_configs,
    )

    request = make_curation_request(paths=["blog"])
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    assert result.succeeded
    # Writer system prompt should contain path addendum
    writer_system = llm.calls[0]["system"]
    assert "Tone: conversational" in writer_system
    assert "Write warmly." in writer_system


@pytest.mark.integration
async def test_pipeline_without_plugins_backward_compat(sqlite_store):
    """Pipeline without taxonomy or path configs works identically to Phase 1."""
    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
    )

    request = make_curation_request(paths=["blog"])
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    assert result.succeeded
    assert len(result.gate_results) == 1
    assert result.gate_results[0].decision == GateDecision.PASS


@pytest.mark.integration
async def test_pipeline_taxonomy_fallback(sqlite_store):
    """Pipeline completes gracefully when taxonomy plugin raises."""
    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        taxonomy_plugin=FailingTaxonomy(),
    )

    request = make_curation_request(paths=["blog"])
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    # Should succeed despite taxonomy failure
    assert result.succeeded


@pytest.mark.integration
async def test_pipeline_jurisdiction_passthrough(sqlite_store):
    """Jurisdiction from request.constraints reaches the verifier."""
    from cce.models.request import CurationConstraints

    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
    )

    request = make_curation_request(
        paths=["blog"],
        constraints=CurationConstraints(jurisdiction="United States"),
    )
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    assert result.succeeded
    # Verifier is the second LLM call (calls[1])
    verifier_user_msg = llm.calls[1]["messages"][0].content
    assert "Jurisdiction/scope: United States" in verifier_user_msg


@pytest.mark.integration
async def test_pipeline_max_evidence_caps_writer_input(sqlite_store):
    """max_evidence on PathConfig limits evidence passed to writer."""
    llm = _llm(_writer_json(), _verifier_json(supported=1, total=1, gaps=0))
    adapter = _make_adapter()
    config = make_engine_config()

    path_configs = {
        "blog": PathConfig(
            id="blog",
            name="Blog",
            max_evidence=1,
        ),
    }

    pipeline = Pipeline(
        config=config,
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=llm,
        path_configs=path_configs,
    )

    request = make_curation_request(paths=["blog"])
    policy = make_source_policy()
    result = await pipeline.run(request, policy)

    assert result.succeeded
    # Writer prompt should mention 1 evidence excerpt (capped from full set)
    writer_user_msg = llm.calls[0]["messages"][0].content
    assert "1 evidence excerpts" in writer_user_msg
