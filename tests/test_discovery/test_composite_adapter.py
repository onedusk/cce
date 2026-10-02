"""Composite crawl adapter: web and non-web sources in one run (B14)."""

from __future__ import annotations

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult
from cce.discovery.adapters.composite import CompositeCrawlAdapter
from cce.discovery.discoverer import Discoverer
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import llm as _llm
from tests.test_orchestrator.conftest import verifier_json

pytestmark = pytest.mark.unit

WEB_URL = "https://example.com/article"
LOCAL_URL = "local://acme/q3-report.pdf"
WEB_TEXT = "The web page says adults need seven or more hours of sleep a night."
LOCAL_TEXT = "The internal report says the Q3 sleep-study cohort had 412 people."


class FakeLocalAdapter:
    """A consumer's non-web adapter: serves and lists ``local://`` documents."""

    def __init__(self, docs: dict[str, str]) -> None:
        self._docs = docs
        self.crawled: list[str] = []

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        self.crawled.append(request.url)
        if request.url not in self._docs:
            return CrawlResult(url=request.url, status_code=0)
        return make_crawl_result(
            url=request.url, title="Local doc", markdown=self._docs[request.url]
        )

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        return [await self.crawl(r) for r in requests]

    async def search(self, query: str, limit: int = 10) -> list[str]:
        return list(self._docs)[:limit]


def _web(urls: list[str] | None = None) -> MockCrawlAdapter:
    urls = urls or [WEB_URL]
    return MockCrawlAdapter(
        search_map={"test topic": urls},
        url_map={u: make_crawl_result(url=u, markdown=WEB_TEXT) for u in urls},
    )


def _composite(local: FakeLocalAdapter | None = None, web=None):
    return CompositeCrawlAdapter(
        {"local": local or FakeLocalAdapter({LOCAL_URL: LOCAL_TEXT})},
        default=web or _web(),
    )


def test_satisfies_the_crawl_adapter_protocol():
    assert isinstance(_composite(), CrawlAdapter)


async def test_discovery_returns_evidence_from_both_adapters():
    """B14 accept: a fake local:// adapter and the web adapter in one run."""
    discoverer = Discoverer(adapter=_composite(), config=CrawlConfig(api_key="k"))
    result = await discoverer.discover(make_curation_request(), make_source_policy())

    by_url = {ev.url: ev.excerpt for ev in result.evidence}
    assert by_url == {WEB_URL: WEB_TEXT, LOCAL_URL: LOCAL_TEXT}
    assert result.metrics["crawl_success"] == 2
    assert result.metrics["urls_dropped_policy"] == 0


@pytest.mark.integration
async def test_pipeline_cites_a_non_web_source(sqlite_store):
    import json

    reply = json.dumps(
        {
            "content": "Web claim [ev:ev_001]. Report claim [ev:ev_002].",
            "citations_used": ["ev_001", "ev_002"],
            "evidence_map": [{"claim": "c", "evidence_ids": ["ev_001", "ev_002"]}],
            "gaps": [],
        }
    )
    pipeline = Pipeline(
        config=make_engine_config(),
        crawl_adapter=_composite(),
        evidence_store=sqlite_store,
        llm=_llm(reply, verifier_json(supported=10, total=10, gaps=0)),
    )
    result = await pipeline.run(make_curation_request(), make_source_policy())

    assert {c.url for c in result.package.units[0].citations} == {WEB_URL, LOCAL_URL}


async def test_policy_host_rules_apply_to_pseudo_urls():
    """The policy matches a pseudo-URL's host like a domain; a URL with no
    host (file:///...) is dropped before it reaches any adapter."""
    local = FakeLocalAdapter(
        {
            LOCAL_URL: LOCAL_TEXT,
            "local://other/notes.md": LOCAL_TEXT + " Other tenant.",
            "file:///etc/passwd": "never crawled, the URL has no host part at all",
        }
    )
    discoverer = Discoverer(
        adapter=CompositeCrawlAdapter({"local": local, "file": local}),
        config=CrawlConfig(api_key="k"),
    )
    result = await discoverer.discover(
        make_curation_request(), make_source_policy(domains_allow=["acme"])
    )

    assert [ev.url for ev in result.evidence] == [LOCAL_URL]
    assert local.crawled == [LOCAL_URL]
    assert result.metrics["urls_dropped_policy"] == 2


async def test_crawl_many_keeps_request_order_and_fails_unknown_schemes():
    local = FakeLocalAdapter({LOCAL_URL: LOCAL_TEXT})
    composite = CompositeCrawlAdapter({"local": local, "https": _web()})
    urls = [LOCAL_URL, "gdrive://abc123", WEB_URL, "local://acme/missing.pdf"]

    results = await composite.crawl_many([CrawlRequest(url=u) for u in urls])

    assert [r.url for r in results] == urls
    assert [r.status_code for r in results] == [200, 0, 200, 0]
    single = await composite.crawl(CrawlRequest(url="gdrive://abc123"))
    assert single.status_code == 0


async def test_crawl_many_tolerates_short_and_long_batches():
    class _Short(FakeLocalAdapter):
        async def crawl_many(self, requests):
            return (await super().crawl_many(requests))[:1]

    class _Long(MockCrawlAdapter):
        async def crawl_many(self, requests):
            extra = make_crawl_result(url="https://example.com/child")
            return [*(await super().crawl_many(requests)), extra]

    docs = {LOCAL_URL: LOCAL_TEXT, "local://acme/b.pdf": LOCAL_TEXT}
    web = _Long(url_map={WEB_URL: make_crawl_result(url=WEB_URL)})
    composite = CompositeCrawlAdapter({"local": _Short(docs)}, default=web)
    urls = [LOCAL_URL, WEB_URL, "local://acme/b.pdf"]

    results = await composite.crawl_many([CrawlRequest(url=u) for u in urls])

    assert [(r.url, r.status_code) for r in results] == [
        (LOCAL_URL, 200),
        (WEB_URL, 200),
        ("local://acme/b.pdf", 0),  # the adapter never returned it
        ("https://example.com/child", 200),  # extra result, appended
    ]


async def test_search_interleaves_adapters_without_duplicates():
    web_urls = [f"https://site{i}.example/p" for i in range(3)]
    local = FakeLocalAdapter({LOCAL_URL: LOCAL_TEXT, "local://acme/b.pdf": "x"})
    composite = _composite(local, _web(web_urls))

    assert await composite.search("test topic") == [
        web_urls[0],
        LOCAL_URL,
        web_urls[1],
        "local://acme/b.pdf",
        web_urls[2],
    ]
    # The web mock has no results for this query (NotImplementedError): the
    # other adapter still answers.
    assert await composite.search("other", limit=1) == [LOCAL_URL]


async def test_search_raises_when_no_adapter_supports_it():
    composite = CompositeCrawlAdapter({}, default=MockCrawlAdapter())
    with pytest.raises(NotImplementedError):
        await composite.search("anything")
