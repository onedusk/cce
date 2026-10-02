"""Evidence reused from the store passes the job's filters (COR-01) and
the chunk-size bound (CRIT-01).

A URL already in the evidence store is not re-crawled: its stored rows join
the run. They used to join after hash dedup only, so a row stored under a
permissive policy reached a later job whose policy or request would have
dropped the same page on a fresh crawl, and a row stored before chunks were
bounded reached every prompt at full size.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import MAX_CHUNK_SIZE, Discoverer
from cce.models.request import CurationConstraints
from cce.policy.types import RecencyRule, ReputationRule
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_evidence,
    make_source_policy,
)
from tests.test_discovery.test_discovery_ledger import assert_ledgers_balance

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
RECENT = (NOW - timedelta(days=30)).isoformat()
OLD = "https://blog.example.com/old-post"
ADS = "https://shop.example.com/promo"
PEER = "https://pubmed.ncbi.nlm.nih.gov/123"
BODY = (
    "A long enough paragraph about sleep and memory consolidation in adults "
    "who rest well every night."
)
LAX = ReputationRule(block_marketing=False)
DROPS = ("dropped_date", "dropped_reputation", "dropped_marketing")


def _adapter() -> MockCrawlAdapter:
    return MockCrawlAdapter(
        search_map={"test topic": [OLD, ADS, PEER]},
        url_map={
            OLD: make_crawl_result(
                url=OLD, markdown=BODY + " old", published_date="2011-03-01"
            ),
            ADS: make_crawl_result(
                url=ADS, markdown="Sponsored content. " + BODY, published_date=RECENT
            ),
            PEER: make_crawl_result(
                url=PEER, markdown=BODY + " peer", published_date=RECENT
            ),
        },
    )


def _discoverer(adapter: MockCrawlAdapter, store=None) -> Discoverer:
    return Discoverer(adapter, CrawlConfig(api_key="test"), evidence_store=store)


@pytest.mark.parametrize(
    ("policy_kw", "constraints", "kept", "drops"),
    [
        pytest.param(
            {"reputation": LAX, "recency": RecencyRule(max_age_days=365)},
            None,
            {ADS, PEER},
            {"dropped_date": 1},
            id="max-age-days",
        ),
        pytest.param(
            {"reputation": ReputationRule(block_marketing=True)},
            None,
            {OLD, PEER},
            {"dropped_marketing": 1},
            id="block-marketing",
        ),
        pytest.param(
            {
                "reputation": ReputationRule(
                    block_marketing=False, require_peer_reviewed=True
                )
            },
            None,
            {PEER},
            {"dropped_reputation": 2},
            id="require-peer-reviewed",
        ),
        pytest.param(
            {
                "reputation": ReputationRule(
                    block_marketing=False, require_primary_source=True
                )
            },
            None,
            {PEER},
            {"dropped_reputation": 2},
            id="require-primary-source",
        ),
        pytest.param(
            {"reputation": LAX},
            CurationConstraints(date_from="2024-01-01T00:00:00+00:00"),
            {ADS, PEER},
            {"dropped_date": 1},
            id="request-date-from",
        ),
    ],
)
async def test_reused_rows_pass_the_same_filters_as_a_fresh_crawl(
    sqlite_store, policy_kw, constraints, kept, drops
):
    request = make_curation_request(constraints=constraints)
    strict = make_source_policy(id="strict", **policy_kw)
    expected_drops = {key: drops.get(key, 0) for key in DROPS}

    # Control: nothing stored, every page crawled under the strict policy.
    fresh = await _discoverer(_adapter()).discover(request, strict)
    assert {ev.url for ev in fresh.evidence} == kept
    assert {key: fresh.metrics[key] for key in DROPS} == expected_drops

    # A permissive job stores all three pages; the strict job then reuses them.
    discoverer = _discoverer(_adapter(), sqlite_store)
    lax = await discoverer.discover(
        make_curation_request(), make_source_policy(id="lax", reputation=LAX)
    )
    assert {ev.url for ev in lax.evidence} == {OLD, ADS, PEER}
    await sqlite_store.put_many(lax.evidence)

    reused = await discoverer.discover(request, strict)

    assert reused.metrics["urls_reused"] == 3
    assert reused.metrics["excerpts_reused"] == 3
    assert reused.metrics["crawl_success"] == 0
    assert {ev.url for ev in reused.evidence} == kept
    assert {key: reused.metrics[key] for key in DROPS} == expected_drops
    assert_ledgers_balance(reused.metrics)


@pytest.mark.parametrize(
    ("policy_kw", "constraints"),
    [
        pytest.param(
            {"recency": RecencyRule(max_age_days=365)}, None, id="max-age-days"
        ),
        pytest.param(
            {},
            CurationConstraints(date_from="2024-01-01T00:00:00+00:00"),
            id="date-from",
        ),
    ],
)
async def test_a_naive_stored_date_is_read_as_utc_on_reuse(
    sqlite_store, policy_kw, constraints
):
    """Rows stored before date-only published dates were read as UTC keep a
    naive published_at; compared with an aware date it failed open."""
    await sqlite_store.put_many(
        [make_evidence(url=OLD, excerpt=BODY, published_at=datetime(2011, 3, 1))]
    )
    [stored] = await sqlite_store.get_by_urls([OLD])
    assert stored.published_at is not None and stored.published_at.tzinfo is None
    adapter = MockCrawlAdapter(search_map={"test topic": [OLD]})

    result = await _discoverer(adapter, sqlite_store).discover(
        make_curation_request(constraints=constraints),
        make_source_policy(**policy_kw),
    )

    assert result.evidence == []
    assert result.metrics["urls_reused"] == 1
    assert result.metrics["dropped_date"] == 1
    assert_ledgers_balance(result.metrics)


async def test_reused_row_age_is_measured_from_now_not_from_its_crawl(sqlite_store):
    """A page that was 10 days old when it was crawled 390 days ago is 400
    days old today: max_age_days=365 drops it on reuse."""
    aged, young = "https://example.org/aged", "https://example.org/young"
    await sqlite_store.put_many(
        [
            make_evidence(
                url=aged,
                excerpt=BODY + " aged",
                published_at=NOW - timedelta(days=400),
                retrieved_at=NOW - timedelta(days=390),
            ),
            make_evidence(
                url=young,
                excerpt=BODY + " young",
                published_at=NOW - timedelta(days=300),
                retrieved_at=NOW - timedelta(days=290),
            ),
        ]
    )
    adapter = MockCrawlAdapter(search_map={"test topic": [aged, young]})

    result = await _discoverer(adapter, sqlite_store).discover(
        make_curation_request(),
        make_source_policy(recency=RecencyRule(max_age_days=365)),
    )

    assert [ev.url for ev in result.evidence] == [young]
    assert result.metrics["urls_reused"] == 2
    assert result.metrics["dropped_date"] == 1
    assert_ledgers_balance(result.metrics)


async def test_an_oversized_stored_row_is_split_on_reuse(sqlite_store):
    """CRIT-01: a store written before chunks were bounded can hold a row of
    any size, and its URL is never crawled again: the row is split on reuse.
    Rows within the bound pass through unchanged."""
    big_url, small_url = "https://example.org/transcript", "https://example.org/a"
    big = make_evidence(
        url=big_url,
        excerpt="w" * 1500 + " " + "v" * 1500 + " end",
        locator="chunk:3",
    )
    small = make_evidence(url=small_url, excerpt=BODY)
    await sqlite_store.put_many([big, small])
    discoverer = Discoverer(
        MockCrawlAdapter(search_map={"test topic": [big_url, small_url]}),
        CrawlConfig(api_key="test"),
        evidence_store=sqlite_store,
    )
    request, policy = make_curation_request(), make_source_policy()

    result = await discoverer.discover(request, policy)

    pieces = [ev for ev in result.evidence if ev.url == big_url]
    assert [ev.excerpt for ev in pieces] == ["w" * 1500, "v" * 1500]
    assert max(len(ev.excerpt) for ev in result.evidence) <= MAX_CHUNK_SIZE
    for ev in pieces:
        assert ev.excerpt in big.excerpt
        assert ev.excerpt_hash == hashlib.sha256(ev.excerpt.encode()).hexdigest()
        assert ev.id != big.id
        assert ev.model_dump(exclude={"id", "excerpt", "excerpt_hash"}) == (
            big.model_dump(exclude={"id", "excerpt", "excerpt_hash"})
        )
    assert len({ev.id for ev in pieces}) == 2
    assert [ev for ev in result.evidence if ev.url == small_url] == [small]
    m = result.metrics
    assert (m["urls_reused"], m["crawl_success"]) == (2, 0)
    # Three chunks of the big row ("end" is a fragment) plus the small row
    assert (m["excerpts_reused"], m["dropped_fragment"], m["kept"]) == (4, 1, 3)
    assert_ledgers_balance(m)

    # The pipeline stores the pieces; a later run dedups the stored copies
    # against the re-split ones, whose IDs resolve to the stored rows.
    await sqlite_store.put_many(result.evidence)
    again = await discoverer.discover(request, policy)

    assert sorted(ev.excerpt for ev in again.evidence) == sorted(
        ev.excerpt for ev in result.evidence
    )
    assert again.metrics["deduplicated"] == 2
    assert_ledgers_balance(again.metrics)
    remap = await sqlite_store.get_stored_ids(again.evidence)
    stored_ids = {ev.id for ev in pieces}
    assert {remap[ev.id] for ev in again.evidence if ev.url == big_url} == stored_ids
