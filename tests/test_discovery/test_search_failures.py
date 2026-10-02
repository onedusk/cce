"""OPS-09: a crawl-provider outage is reported as one, not as an empty topic.

FirecrawlAdapter.search used to swallow every error and return [], so a bad
key, exhausted credits or an outage ended the job as "No evidence
discovered" with metrics identical to a topic with no sources.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from firecrawl.v2.utils.error_handler import PaymentRequiredError

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlResult
from cce.discovery.adapters.composite import CompositeCrawlAdapter
from cce.discovery.adapters.firecrawl import FirecrawlAdapter
from cce.discovery.discoverer import Discoverer
from cce.models.job import JobStage, JobStatus
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_discovery.test_discovery_ledger import assert_ledgers_balance
from tests.test_orchestrator.conftest import llm as _llm

pytestmark = pytest.mark.unit

PROVIDER_TEXT = "Payment Required: insufficient credits for account acct_secret_42"


class _RaisingSearch(MockCrawlAdapter):
    """Search raises a provider error for every query."""

    def __init__(self, error: BaseException, **kw) -> None:
        super().__init__(**kw)
        self._error = error
        self.searched: list[str] = []

    async def search(self, query: str, limit: int = 10) -> list[str]:
        self.searched.append(query)
        raise self._error


def _firecrawl(search_error: BaseException | None = None) -> FirecrawlAdapter:
    """The real adapter with its SDK client stubbed (no network)."""
    with patch("cce.discovery.adapters.firecrawl.FirecrawlApp") as fc_cls:
        client = MagicMock()
        client.search = MagicMock(side_effect=search_error)
        fc_cls.return_value = client
        return FirecrawlAdapter(CrawlConfig(api_key="test-ops09-key"))


async def test_firecrawl_search_raises_instead_of_returning_empty():
    adapter = _firecrawl(PaymentRequiredError(PROVIDER_TEXT, status_code=402))
    with pytest.raises(PaymentRequiredError):
        await adapter.search("sleep")


async def test_failed_searches_are_counted_with_the_error_class_only():
    adapter = _RaisingSearch(PaymentRequiredError(PROVIDER_TEXT, status_code=402))
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))
    request = make_curation_request(subtopics=["a", "b"])

    result = await discoverer.discover(request, make_source_policy())

    assert len(adapter.searched) == 3  # every query was still tried
    assert result.metrics["search_failed"] == 3
    assert result.metrics["search_error"] == "PaymentRequiredError"
    assert all("acct_secret_42" not in str(v) for v in result.metrics.values())
    assert_ledgers_balance(result.metrics)


async def test_one_failed_query_keeps_the_others_results():
    class _FailsOnce(MockCrawlAdapter):
        async def search(self, query: str, limit: int = 10) -> list[str]:
            if query == "test topic a":
                raise TimeoutError("read timed out")
            return await super().search(query, limit)

    url = "https://example.com/article"
    adapter = _FailsOnce(
        search_map={"test topic": [url]}, url_map={url: make_crawl_result(url=url)}
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))

    result = await discoverer.discover(
        make_curation_request(subtopics=["a"]), make_source_policy()
    )

    assert [ev.url for ev in result.evidence] == [url, url]
    assert result.metrics["search_failed"] == 1
    assert result.metrics["search_error"] == "TimeoutError"


async def test_adapter_without_search_is_not_a_failure():
    """NotImplementedError still means 'no search', as before."""
    discoverer = Discoverer(adapter=MockCrawlAdapter(), config=CrawlConfig(api_key="k"))

    result = await discoverer.discover(make_curation_request(), make_source_policy())

    assert result.metrics["search_failed"] == 0
    assert "search_error" not in result.metrics


async def test_composite_keeps_other_adapters_when_one_search_fails():
    class _Local(MockCrawlAdapter):
        async def search(self, query: str, limit: int = 10) -> list[str]:
            return ["local://acme/a.pdf"]

    web = _RaisingSearch(PaymentRequiredError(PROVIDER_TEXT))
    composite = CompositeCrawlAdapter({"local": _Local()}, default=web)

    assert await composite.search("q") == ["local://acme/a.pdf"]


async def test_composite_raises_when_a_search_failed_and_nothing_was_found():
    class _Empty(MockCrawlAdapter):
        async def search(self, query: str, limit: int = 10) -> list[str]:
            return []

    web = _RaisingSearch(PaymentRequiredError(PROVIDER_TEXT))
    composite = CompositeCrawlAdapter({"local": _Empty()}, default=web)

    with pytest.raises(PaymentRequiredError):
        await composite.search("q")


@pytest.mark.integration
async def test_search_outage_fails_the_job_as_crawl_unavailable(sqlite_store):
    adapter = _firecrawl(PaymentRequiredError(PROVIDER_TEXT, status_code=402))
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=_llm(),
    )

    result = await pipeline.run(make_curation_request(), make_source_policy())

    job = result.job
    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert job.error.code == "crawl_unavailable"
    assert job.error.stage == JobStage.DISCOVER
    assert job.error.message.startswith("No evidence discovered")
    assert "PaymentRequiredError" in job.error.message
    assert "check the crawl provider" in job.error.message
    assert "acct_secret_42" not in job.error.message
    [record] = job.stages
    assert record.metrics is not None
    assert record.metrics["search_failed"] == 1


@pytest.mark.integration
async def test_every_crawl_failing_fails_the_job_as_crawl_unavailable(sqlite_store):
    urls = ["https://a.example/1", "https://b.example/2"]
    adapter = MockCrawlAdapter(
        search_map={"test topic": urls},
        url_map={u: CrawlResult(url=u, status_code=0) for u in urls},
    )
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=_llm(),
    )

    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.job.error is not None
    assert result.job.error.code == "crawl_unavailable"
    assert "2 crawls failed" in result.job.error.message
    # Failed pages (unreachable or HTTP errors) are not blamed on the
    # provider's key or credits.
    assert "check the crawl provider" not in result.job.error.message


@pytest.mark.integration
async def test_empty_topic_with_a_healthy_provider_keeps_pipeline_error(sqlite_store):
    adapter = MockCrawlAdapter(search_map={"test topic": []})
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=adapter,
        evidence_store=sqlite_store,
        llm=_llm(),
    )

    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert result.job.error is not None
    assert result.job.error.code == "pipeline_error"
    assert result.job.error.message == "No evidence discovered"
