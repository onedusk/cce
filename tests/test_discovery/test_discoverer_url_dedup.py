"""Tests for Discoverer pre-crawl URL dedup (audit P3, task T-02.02).

Covers `_split_fresh_and_reusable` which both filters already-crawled URLs
off the crawl path (cost win) AND pulls their stored evidence back into the
current run (retry-semantics preservation).
"""

from __future__ import annotations

import logging

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import Discoverer
from tests.conftest import make_evidence

pytestmark = pytest.mark.integration


class _StubEvidenceStore:
    """Minimal stub — implements only the two methods the dedup path calls."""

    def __init__(self, stored: dict[str, list]) -> None:
        """stored: {url: [Evidence, ...]} — what get_by_urls should return."""
        self._stored = stored
        self.get_existing_calls: list[list[str]] = []
        self.get_by_urls_calls: list[list[str]] = []

    async def get_existing_urls(self, candidates: list[str]) -> set[str]:
        self.get_existing_calls.append(list(candidates))
        return {u for u in candidates if u in self._stored}

    async def get_by_urls(self, urls: list[str]) -> list:
        self.get_by_urls_calls.append(list(urls))
        result = []
        for u in urls:
            result.extend(self._stored.get(u, []))
        return result


def _build_discoverer(store: _StubEvidenceStore | None) -> Discoverer:
    return Discoverer(
        adapter=None,  # type: ignore[arg-type]  # not used by the helper
        config=CrawlConfig(api_key="test"),
        evidence_store=store,  # type: ignore[arg-type]  # structural typing
    )


async def test_no_overlap_returns_all_fresh_and_no_reusable():
    store = _StubEvidenceStore(stored={})
    discoverer = _build_discoverer(store)
    candidates = [f"https://example.com/{i}" for i in range(3)]

    fresh, reusable = await discoverer._split_fresh_and_reusable(candidates)

    assert fresh == candidates
    assert reusable == []
    assert store.get_by_urls_calls == []  # short-circuit — no reusable query


async def test_partial_overlap_splits_fresh_and_reusable():
    ev_a = make_evidence(
        id="ev_a", url="https://a.example.com", excerpt="Excerpt A distinct content."
    )
    ev_b = make_evidence(
        id="ev_b", url="https://b.example.com", excerpt="Excerpt B distinct content."
    )
    store = _StubEvidenceStore(
        stored={"https://a.example.com": [ev_a], "https://b.example.com": [ev_b]}
    )
    discoverer = _build_discoverer(store)
    candidates = [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
        "https://d.example.com",
    ]

    fresh, reusable = await discoverer._split_fresh_and_reusable(candidates)

    assert fresh == [
        "https://c.example.com",
        "https://d.example.com",
    ]  # order preserved
    assert {ev.id for ev in reusable} == {"ev_a", "ev_b"}


async def test_full_overlap_returns_empty_fresh_and_all_reusable():
    ev_a = make_evidence(
        id="ev_a", url="https://a.example.com", excerpt="Excerpt A distinct content."
    )
    ev_b = make_evidence(
        id="ev_b", url="https://b.example.com", excerpt="Excerpt B distinct content."
    )
    store = _StubEvidenceStore(
        stored={"https://a.example.com": [ev_a], "https://b.example.com": [ev_b]}
    )
    discoverer = _build_discoverer(store)

    fresh, reusable = await discoverer._split_fresh_and_reusable(
        ["https://a.example.com", "https://b.example.com"]
    )

    assert fresh == []
    assert {ev.id for ev in reusable} == {"ev_a", "ev_b"}


async def test_multiple_evidence_per_url_all_returned():
    ev_a1 = make_evidence(
        id="ev_a1", url="https://a.example.com", excerpt="Excerpt A1 content."
    )
    ev_a2 = make_evidence(
        id="ev_a2", url="https://a.example.com", excerpt="Excerpt A2 content."
    )
    store = _StubEvidenceStore(stored={"https://a.example.com": [ev_a1, ev_a2]})
    discoverer = _build_discoverer(store)

    fresh, reusable = await discoverer._split_fresh_and_reusable(
        ["https://a.example.com"]
    )

    assert fresh == []
    assert {ev.id for ev in reusable} == {"ev_a1", "ev_a2"}


async def test_empty_candidates_short_circuits():
    store = _StubEvidenceStore(stored={})
    discoverer = _build_discoverer(store)

    fresh, reusable = await discoverer._split_fresh_and_reusable([])

    assert fresh == []
    assert reusable == []
    assert store.get_existing_calls == []  # did not even query


async def test_no_store_is_no_op():
    discoverer = _build_discoverer(None)
    candidates = ["https://a.example.com", "https://b.example.com"]

    fresh, reusable = await discoverer._split_fresh_and_reusable(candidates)

    assert fresh == candidates
    assert reusable == []


async def test_log_emitted_when_any_skipped(caplog):
    ev_a = make_evidence(
        id="ev_a", url="https://a.example.com", excerpt="Excerpt A content."
    )
    store = _StubEvidenceStore(stored={"https://a.example.com": [ev_a]})
    discoverer = _build_discoverer(store)

    with caplog.at_level(logging.INFO, logger="cce.discovery.discoverer"):
        await discoverer._split_fresh_and_reusable(
            ["https://a.example.com", "https://b.example.com"]
        )

    dedup_lines = [r for r in caplog.records if "URL dedup" in r.message]
    assert len(dedup_lines) == 1
    msg = dedup_lines[0].getMessage()
    assert "1/2" in msg
    assert "reusing 1" in msg


async def test_log_not_emitted_when_nothing_skipped(caplog):
    store = _StubEvidenceStore(stored={})
    discoverer = _build_discoverer(store)

    with caplog.at_level(logging.INFO, logger="cce.discovery.discoverer"):
        await discoverer._split_fresh_and_reusable(["https://a.example.com"])

    dedup_lines = [r for r in caplog.records if "URL dedup" in r.message]
    assert dedup_lines == []


# ---------------------------------------------------------------------------
# F-3: policy.max_sources_per_run applies to fresh + reusable combined
# ---------------------------------------------------------------------------


async def _run_discover_with_cap(
    *,
    max_sources_per_run: int,
    fresh_urls: list[str],
    stored: dict[str, list],
):
    """Drive Discoverer.discover() end-to-end with seeded fresh + stored URLs.

    Returns the `(fresh_urls_sent_to_crawl, final_evidence_url_set)`.
    """
    from tests.conftest import (
        MockCrawlAdapter,
        make_crawl_result,
        make_curation_request,
        make_source_policy,
    )

    # Crawl adapter returns every `fresh_urls` entry from search. Markdown is
    # unique per URL so excerpt-hash dedup in _extract_evidence doesn't collapse
    # multiple fresh URLs into one.
    adapter = MockCrawlAdapter(
        search_map={"test topic": fresh_urls},
        url_map={
            url: make_crawl_result(
                url=url,
                markdown=(
                    f"A reasonably long paragraph for {url} to exceed the "
                    "fifty-character minimum for evidence extraction; unique."
                ),
            )
            for url in fresh_urls
        },
    )

    store = _StubEvidenceStore(stored=stored)
    discoverer = Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test"),
        evidence_store=store,  # type: ignore[arg-type]
    )
    policy = make_source_policy(max_sources_per_run=max_sources_per_run)
    request = make_curation_request(topic="test topic")

    evidence = (await discoverer.discover(request, policy)).evidence
    # Count unique URLs in the final evidence — one "source" per URL.
    return {ev.url for ev in evidence}


async def test_cap_applies_to_fresh_plus_reusable_combined():
    """max=5; 10 fresh + 10 reusable -> total unique sources <= 5."""
    fresh = [f"https://fresh.example.com/{i}" for i in range(10)]
    reusable_urls = [f"https://stored.example.com/{i}" for i in range(10)]
    stored = {
        u: [
            make_evidence(
                id=f"ev_{u.rsplit('/', 1)[-1]}",
                url=u,
                excerpt=f"Stored excerpt for {u} — distinct content.",
            )
        ]
        for u in reusable_urls
    }

    source_urls = await _run_discover_with_cap(
        max_sources_per_run=5, fresh_urls=fresh + reusable_urls, stored=stored
    )

    assert len(source_urls) <= 5, (
        f"Expected <=5 unique source URLs post-cap; got {len(source_urls)}"
    )


async def test_cap_fresh_priority_over_reusable():
    """max=3; 4 fresh + 10 reusable -> fresh wins: 3 fresh, 0 reusable."""
    fresh = [f"https://fresh.example.com/{i}" for i in range(4)]
    reusable_urls = [f"https://stored.example.com/{i}" for i in range(10)]
    stored = {
        u: [make_evidence(id=f"ev_{u.rsplit('/', 1)[-1]}", url=u, excerpt=f"x{u}")]
        for u in reusable_urls
    }

    source_urls = await _run_discover_with_cap(
        max_sources_per_run=3, fresh_urls=fresh + reusable_urls, stored=stored
    )

    # All 3 source slots went to fresh URLs. No reusable URL appears.
    assert len(source_urls) == 3
    assert all(u in set(fresh) for u in source_urls), (
        f"Expected all sources to be fresh; got {source_urls}"
    )


async def test_cap_reusable_fills_remainder():
    """max=5; 2 fresh + 10 reusable -> 2 fresh + 3 reusable = 5 total."""
    fresh = [f"https://fresh.example.com/{i}" for i in range(2)]
    reusable_urls = [f"https://stored.example.com/{i}" for i in range(10)]
    stored = {
        u: [
            make_evidence(
                id=f"ev_{u.rsplit('/', 1)[-1]}",
                url=u,
                excerpt=f"Stored excerpt for {u} unique long enough content.",
            )
        ]
        for u in reusable_urls
    }

    source_urls = await _run_discover_with_cap(
        max_sources_per_run=5, fresh_urls=fresh + reusable_urls, stored=stored
    )

    # 2 fresh (crawled) + 3 reusable = 5 total unique source URLs.
    assert len(source_urls) == 5
    fresh_in_final = source_urls & set(fresh)
    reusable_in_final = source_urls & set(reusable_urls)
    assert len(fresh_in_final) == 2
    assert len(reusable_in_final) == 3
