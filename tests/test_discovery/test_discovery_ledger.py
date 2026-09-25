"""Every discovery drop is counted by reason (B10).

Two ledgers, each summing exactly:

- URLs: urls_gathered == urls_dropped_policy + urls_capped + urls_reused
  + crawl_failed + crawl_success
- Excerpts: excerpts_gathered == dropped_fragment + dropped_date
  + dropped_reputation + dropped_marketing + deduplicated + capped + kept
"""

from __future__ import annotations

import logging

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest, CrawlResult
from cce.discovery.discoverer import Discoverer
from cce.models.job import DiscoverMetrics
from cce.policy.types import RecencyRule, ReputationRule
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_evidence,
    make_source_policy,
)

pytestmark = pytest.mark.unit

URL_DROPS = (
    "urls_dropped_policy",
    "urls_capped",
    "urls_reused",
    "crawl_failed",
    "crawl_success",
)
EXCERPT_DROPS = (
    "dropped_fragment",
    "dropped_date",
    "dropped_reputation",
    "dropped_marketing",
    "deduplicated",
    "capped",
    "kept",
)


def _para(word: str) -> str:
    return f"The {word} paragraph carries enough words to clear the fragment floor."


P1, P2, P3, P4, P5, P6, P7, P8 = (
    _para(w)
    for w in (
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
    )
)
STORED_UNIQUE = _para("stored")


class _StubStore:
    """Reports ``stored`` URLs as indexed; rows come back in URL order."""

    def __init__(self, stored: dict[str, list]) -> None:
        self._stored = stored

    async def get_existing_urls(self, candidates: list[str]) -> set[str]:
        return {u for u in candidates if u in self._stored}

    async def get_by_urls(self, urls: list[str]) -> list:
        return [ev for u in sorted(urls) for ev in self._stored[u]]


def _page(url: str, *paras: str, **kw) -> CrawlResult:
    return make_crawl_result(url=url, markdown="\n\n".join(paras), **kw)


def assert_ledgers_balance(metrics: dict) -> None:
    assert set(DiscoverMetrics.__annotations__) <= set(metrics)
    assert metrics["urls_gathered"] == sum(metrics[k] for k in URL_DROPS)
    assert metrics["excerpts_gathered"] == sum(metrics[k] for k in EXCERPT_DROPS)


def _discoverer(adapter, store=None, **crawl) -> Discoverer:
    return Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test", **crawl),
        evidence_store=store,  # type: ignore[arg-type]  # structural stub
    )


async def test_every_drop_reason_is_counted_and_both_ledgers_balance(caplog):
    candidates = [
        "https://a.gov/page",
        "https://b.gov/syndicated",
        "https://old.gov/page",
        "https://blog.com/post",
        "https://ads.gov/deal",
        "https://fail.gov/",
        "https://denied.com/x",
        "https://stored.gov/1",
        "https://stored.gov/2",
        "https://ghost.gov/",
    ]
    adapter = MockCrawlAdapter(
        search_map={"test topic": candidates},
        url_map={
            # 4 chunks: one below the fragment floor, three kept pre-cap
            "https://a.gov/page": _page("https://a.gov/page", P1, P2, P3, "Short."),
            # P1 again: a syndicated copy, deduplicated by excerpt hash
            "https://b.gov/syndicated": _page("https://b.gov/syndicated", P1, P4),
            "https://old.gov/page": _page(
                "https://old.gov/page", P5, P6, published_date="1990-01-01T00:00:00Z"
            ),
            "https://blog.com/post": _page("https://blog.com/post", P7),
            "https://ads.gov/deal": _page(
                "https://ads.gov/deal", P8, title="Sponsored: a sleep deal"
            ),
            "https://fail.gov/": CrawlResult(url="https://fail.gov/", status_code=0),
        },
    )
    store = _StubStore(
        {
            # Kept (first reused URL); one row duplicates a fresh excerpt
            "https://stored.gov/1": [
                make_evidence(url="https://stored.gov/1", excerpt=P1),
                make_evidence(url="https://stored.gov/1", excerpt=STORED_UNIQUE),
            ],
            # Past the max_sources_per_run headroom: capped
            "https://stored.gov/2": [
                make_evidence(url="https://stored.gov/2", excerpt=_para("capped"))
            ],
            # Indexed but no rows: reused with zero excerpts
            "https://ghost.gov/": [],
        }
    )
    policy = make_source_policy(
        domains_deny=["denied.com"],
        reputation=ReputationRule(require_primary_source=True, block_marketing=True),
        recency=RecencyRule(max_age_days=3650, prefer_recent=False),
        max_sources_per_run=7,
    )

    with caplog.at_level(logging.INFO, logger="cce.discovery.discoverer"):
        result = await _discoverer(adapter, store, max_excerpts_per_source=1).discover(
            make_curation_request(), policy
        )

    m = result.metrics
    assert {k: m[k] for k in ("urls_gathered", *URL_DROPS)} == {
        "urls_gathered": 10,
        "urls_dropped_policy": 1,
        "urls_capped": 1,
        "urls_reused": 2,
        "crawl_failed": 1,
        "crawl_success": 5,
    }
    assert {
        k: m[k] for k in ("excerpts_gathered", "excerpts_reused", *EXCERPT_DROPS)
    } == {
        "excerpts_gathered": 12,
        "excerpts_reused": 2,
        "dropped_fragment": 1,
        "dropped_date": 2,
        "dropped_reputation": 1,
        "dropped_marketing": 1,
        "deduplicated": 2,
        "capped": 2,
        "kept": 3,
    }
    assert m["kept"] == len(result.evidence)
    assert_ledgers_balance(m)
    [line] = [r.message for r in caplog.records if "Discovery drops" in r.message]
    for key in ("urls_dropped_policy=1", "urls_capped=1", "crawl_failed=1"):
        assert key in line  # URL-level drops too (final review)
    assert "dropped_marketing=1" in line and "deduplicated=2" in line
    assert "capped=2" in line


async def test_fresh_urls_past_the_source_cap_are_counted():
    urls = [f"https://site{i}.gov/page" for i in range(3)]
    adapter = MockCrawlAdapter(
        search_map={"test topic": urls},
        url_map={u: _page(u, _para(u)) for u in urls},
    )
    result = await _discoverer(adapter).discover(
        make_curation_request(), make_source_policy(max_sources_per_run=2)
    )

    assert result.metrics["urls_capped"] == 1
    assert result.metrics["crawl_success"] == 2
    assert result.metrics["kept"] == 2
    assert_ledgers_balance(result.metrics)


async def test_crawl_results_missing_from_the_adapter_count_as_failed():
    class _Lossy(MockCrawlAdapter):
        async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
            return (await super().crawl_many(requests))[:1]

    urls = ["https://one.gov/", "https://two.gov/"]
    adapter = _Lossy(
        search_map={"test topic": urls},
        url_map={u: _page(u, _para(u)) for u in urls},
    )
    result = await _discoverer(adapter).discover(
        make_curation_request(), make_source_policy()
    )

    assert result.metrics["crawl_success"] == 1
    assert result.metrics["crawl_failed"] == 1
    assert result.metrics["crawl_failure_rate"] == 0.5
    assert_ledgers_balance(result.metrics)


@pytest.mark.parametrize(
    ("search", "policy_kw", "store", "expected"),
    [
        pytest.param([], {}, None, {}, id="no-search-results"),
        pytest.param(
            ["https://denied.com/x"],
            {"domains_deny": ["denied.com"]},
            None,
            {"urls_gathered": 1, "urls_dropped_policy": 1},
            id="all-denied",
        ),
        pytest.param(
            ["https://a.gov/page"],
            {"max_sources_per_run": 0},
            None,
            {"urls_gathered": 1, "urls_capped": 1},
            id="source-cap-zero",
        ),
        pytest.param(
            ["https://stored.gov/1"],
            {"max_sources_per_run": 0},
            {"https://stored.gov/1": [make_evidence(url="https://stored.gov/1")]},
            {"urls_gathered": 1, "urls_capped": 1},
            id="reused-url-capped",
        ),
    ],
)
async def test_early_return_carries_the_full_ledger(search, policy_kw, store, expected):
    adapter = MockCrawlAdapter(search_map={"test topic": search})
    result = await _discoverer(
        adapter, _StubStore(store) if store is not None else None
    ).discover(make_curation_request(), make_source_policy(**policy_kw))

    assert result.evidence == []
    assert_ledgers_balance(result.metrics)
    nonzero = {k: v for k, v in result.metrics.items() if v}
    assert nonzero == expected


@pytest.mark.parametrize("shape", ["extra-child-page", "every-result-twice"])
async def test_adapter_returning_more_results_keeps_the_url_ledger_exact(shape):
    """Final review of B10: extra or duplicate results counted as extra
    crawl successes, so the URL ledger summed past urls_gathered."""

    class _Generous(MockCrawlAdapter):
        async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
            results = await super().crawl_many(requests)
            if shape == "every-result-twice":
                return results + results
            return results + [_page("https://child.gov/p", _para("child"))]

    urls = ["https://one.gov/", "https://two.gov/"]
    adapter = _Generous(
        search_map={"test topic": urls},
        url_map={u: _page(u, _para(u)) for u in urls},
    )
    result = await _discoverer(adapter).discover(
        make_curation_request(), make_source_policy()
    )

    assert result.metrics["crawl_success"] == 2
    assert result.metrics["crawl_failed"] == 0
    assert_ledgers_balance(result.metrics)


async def test_early_return_logs_its_drops(caplog):
    adapter = MockCrawlAdapter(search_map={"test topic": ["https://denied.com/x"]})
    with caplog.at_level(logging.INFO, logger="cce.discovery.discoverer"):
        await _discoverer(adapter).discover(
            make_curation_request(), make_source_policy(domains_deny=["denied.com"])
        )

    assert "Discovery drops: urls_dropped_policy=1" in caplog.text


def test_negative_source_cap_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="max_sources_per_run"):
        make_source_policy(max_sources_per_run=-1)
