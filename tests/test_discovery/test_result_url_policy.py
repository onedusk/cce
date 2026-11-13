"""SEC-03: the source policy and the request's domain lists are checked on
the URL each crawl result reports, not only on the candidate URLs.

An adapter may report a redirected URL or add child pages (B10); those
hosts never met domains_allow / domains_deny and became evidence.
"""

from __future__ import annotations

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest, CrawlResult
from cce.discovery.discoverer import Discoverer
from cce.models.request import CurationConstraints
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)
from tests.test_discovery.test_discovery_ledger import assert_ledgers_balance

pytestmark = pytest.mark.unit

GOOD = "https://good.example/a"
REDIRECTED = "https://good.example/moved"
LANDING = "https://evil.example/landing"
CHILD = "https://evil.example/child"
SEED = "https://good.example"  # a request allow entry is also a seed URL


def _page(url: str) -> CrawlResult:
    return make_crawl_result(
        url=url,
        markdown=f"A page body for {url}, long enough to pass the fragment floor.",
    )


class _RedirectingAdapter(MockCrawlAdapter):
    """Reports REDIRECTED under another host and appends a child page."""

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        results = [_page(LANDING if r.url == REDIRECTED else r.url) for r in requests]
        return [*results, _page(CHILD)]


async def _discover(constraints=None, **policy_kw):
    adapter = _RedirectingAdapter(search_map={"test topic": [GOOD, REDIRECTED]})
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))
    result = await discoverer.discover(
        make_curation_request(constraints=constraints),
        make_source_policy(**policy_kw),
    )
    assert_ledgers_balance(result.metrics)
    return {ev.url for ev in result.evidence}, result.metrics


@pytest.mark.parametrize(
    "policy_kw",
    [{"domains_allow": ["good.example"]}, {"domains_deny": ["evil.example"]}],
    ids=["not-on-allow-list", "on-deny-list"],
)
async def test_result_url_outside_the_policy_is_not_evidence(policy_kw, caplog):
    kept, metrics = await _discover(**policy_kw)

    assert kept == {GOOD}
    for url in (LANDING, CHILD):  # named, since the job only reports a count
        assert f"outside the source policy: '{url}'" in caplog.text
    # The redirected request yielded no usable page: a failed crawl.
    assert metrics["crawl_success"] == 1
    assert metrics["crawl_failed"] == 1
    assert metrics["urls_dropped_policy"] == 0


@pytest.mark.parametrize(
    ("constraints", "expected"),
    [
        (CurationConstraints(domains_deny=["evil.example"]), {GOOD}),
        (CurationConstraints(domains_allow=["good.example"]), {GOOD, SEED}),
    ],
    ids=["request-deny", "request-allow"],
)
async def test_result_url_outside_the_request_constraints_is_not_evidence(
    constraints, expected
):
    kept, metrics = await _discover(constraints)

    assert kept == expected
    assert metrics["crawl_success"] == len(expected)
    assert metrics["crawl_failed"] == 1


async def test_redirect_and_child_page_the_policy_admits_are_still_evidence():
    kept, metrics = await _discover()

    assert kept == {GOOD, LANDING, CHILD}
    assert metrics["crawl_success"] == 2
    assert metrics["crawl_failed"] == 0
