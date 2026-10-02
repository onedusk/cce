"""Tests for crawl failure tracking in the discoverer."""

from __future__ import annotations

import logging

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlResult
from cce.discovery.discoverer import Discoverer
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)

pytestmark = pytest.mark.unit


def _config() -> CrawlConfig:
    return CrawlConfig(
        adapter="firecrawl", api_key="test", rate_limit_rps=10.0, timeout_seconds=5
    )


class TestCrawlFailureTracking:
    async def test_all_success_no_warning(self, caplog):
        adapter = MockCrawlAdapter(
            search_map={"test topic": ["https://a.com", "https://b.com"]},
            url_map={
                "https://a.com": make_crawl_result(url="https://a.com"),
                "https://b.com": make_crawl_result(url="https://b.com"),
            },
        )
        discoverer = Discoverer(adapter=adapter, config=_config())
        request = make_curation_request(topic="test topic")
        policy = make_source_policy()

        with caplog.at_level(logging.WARNING, logger="cce.discovery.discoverer"):
            result = await discoverer.discover(request, policy)

        assert result.metrics["crawl_failed"] == 0
        assert result.metrics["crawl_success"] == 2
        assert result.metrics["crawl_failure_rate"] == 0.0
        # No warning should be logged
        assert not any(
            "crawl failure rate" in r.message.lower() for r in caplog.records
        )

    async def test_50_percent_failure_warns(self, caplog):
        adapter = MockCrawlAdapter(
            search_map={"test topic": ["https://ok.com", "https://fail.com"]},
            url_map={
                "https://ok.com": make_crawl_result(url="https://ok.com"),
                "https://fail.com": CrawlResult(url="https://fail.com", status_code=0),
            },
        )
        discoverer = Discoverer(adapter=adapter, config=_config())
        request = make_curation_request(topic="test topic")
        policy = make_source_policy()

        with caplog.at_level(logging.WARNING, logger="cce.discovery.discoverer"):
            result = await discoverer.discover(request, policy)

        assert result.metrics["crawl_failed"] == 1
        assert result.metrics["crawl_success"] == 1
        assert result.metrics["crawl_failure_rate"] == 0.5
        assert any("crawl failure rate" in r.message.lower() for r in caplog.records)

    async def test_100_percent_failure(self, caplog):
        adapter = MockCrawlAdapter(
            search_map={"test topic": ["https://fail1.com", "https://fail2.com"]},
            url_map={
                "https://fail1.com": CrawlResult(
                    url="https://fail1.com", status_code=0
                ),
                "https://fail2.com": CrawlResult(
                    url="https://fail2.com", status_code=0
                ),
            },
        )
        discoverer = Discoverer(adapter=adapter, config=_config())
        request = make_curation_request(topic="test topic")
        policy = make_source_policy()

        with caplog.at_level(logging.WARNING, logger="cce.discovery.discoverer"):
            result = await discoverer.discover(request, policy)
        evidence = result.evidence

        assert evidence == []
        assert result.metrics["crawl_failed"] == 2
        assert result.metrics["crawl_failure_rate"] == 1.0
        assert any("crawl failure rate" in r.message.lower() for r in caplog.records)

    async def test_no_urls_after_filter(self):
        """Early return when no URLs survive policy filter — no crawls attempted."""
        adapter = MockCrawlAdapter(
            search_map={"test topic": []},
            url_map={},
        )
        discoverer = Discoverer(adapter=adapter, config=_config())
        request = make_curation_request(topic="test topic")
        policy = make_source_policy()

        result = await discoverer.discover(request, policy)
        evidence = result.evidence

        assert evidence == []
        assert result.metrics["crawl_success"] == 0
        assert result.metrics["crawl_failed"] == 0
        assert result.metrics["crawl_failure_rate"] == 0.0

    async def test_success_status_but_empty_content(self):
        """HTTP 200 with empty markdown counts as a crawl failure."""
        adapter = MockCrawlAdapter(
            search_map={"test topic": ["https://empty.com"]},
            url_map={
                "https://empty.com": CrawlResult(
                    url="https://empty.com", status_code=200, markdown=""
                ),
            },
        )
        discoverer = Discoverer(adapter=adapter, config=_config())
        request = make_curation_request(topic="test topic")
        policy = make_source_policy()

        result = await discoverer.discover(request, policy)
        evidence = result.evidence

        assert evidence == []
        assert result.metrics["crawl_failed"] == 1
        assert result.metrics["crawl_success"] == 0


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
async def test_http_error_page_is_a_failed_crawl_not_evidence(status):
    """COR-03: a page answering 4xx/5xx is a failed crawl: never extracted,
    counted in crawl_failed, and both ledgers stay exact."""
    from tests.test_discovery.test_discovery_ledger import assert_ledgers_balance

    error_body = (
        f"# {status} Error\n\nThe page you requested could not be served. "
        "Please try again later or contact the site administrator."
    )
    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://ok.com/a", "https://err.com/b"]},
        url_map={
            "https://ok.com/a": make_crawl_result(url="https://ok.com/a"),
            "https://err.com/b": make_crawl_result(
                url="https://err.com/b", status_code=status, markdown=error_body
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=_config())

    result = await discoverer.discover(
        make_curation_request(topic="test topic"), make_source_policy()
    )

    assert {ev.url for ev in result.evidence} == {"https://ok.com/a"}
    assert result.metrics["crawl_success"] == 1
    assert result.metrics["crawl_failed"] == 1
    assert_ledgers_balance(result.metrics)


async def test_2xx_and_3xx_pages_are_still_extracted():
    """Only status 0 and >= 400 are failures; other codes are unchanged."""
    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://a.com/x", "https://b.com/y"]},
        url_map={
            "https://a.com/x": make_crawl_result(
                url="https://a.com/x", status_code=203
            ),
            "https://b.com/y": make_crawl_result(
                url="https://b.com/y",
                status_code=304,
                markdown="A different page body, long enough to be kept as evidence.",
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=_config())

    result = await discoverer.discover(
        make_curation_request(topic="test topic"), make_source_policy()
    )

    assert {ev.url for ev in result.evidence} == {"https://a.com/x", "https://b.com/y"}
    assert result.metrics["crawl_failed"] == 0
