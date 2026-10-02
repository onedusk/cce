"""CR-03 / COR-04: request-level CurationConstraints filter discovery.

domains_deny was read nowhere, domains_allow only added seed URLs, and a
date-only date_from / date_to parsed as a naive datetime, so comparing it
with the (aware) published date raised TypeError and the bound failed open.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import Discoverer
from cce.models.request import CurationConstraints
from cce.policy.types import ReputationRule
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)
from tests.test_discovery.test_discovery_ledger import assert_ledgers_balance

pytestmark = pytest.mark.unit

BAD = "https://competitor.example/post"
GOOD = "https://good.example/paper"
OTHER = "https://other.example/study"
LAX = ReputationRule(block_marketing=False, trusted_institutions=[])


class _RecordingAdapter(MockCrawlAdapter):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.crawled: list[str] = []

    async def crawl(self, request):
        self.crawled.append(request.url)
        return await super().crawl(request)


def _adapter() -> _RecordingAdapter:
    tail = " page body, long enough to pass the fragment minimum as evidence."
    pages = {
        BAD: ("Competitor" + tail, "2015-06-01"),
        GOOD: ("Good" + tail, "2023-06-01"),
        OTHER: ("Other" + tail, "2019-06-01"),
    }
    return _RecordingAdapter(
        search_map={"test topic": list(pages)},
        url_map={
            url: make_crawl_result(url=url, markdown=body, published_date=date)
            for url, (body, date) in pages.items()
        },
    )


async def _discover(constraints=None, **policy_kw):
    adapter = _adapter()
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))
    result = await discoverer.discover(
        make_curation_request(constraints=constraints),
        make_source_policy(reputation=LAX, **policy_kw),
    )
    assert_ledgers_balance(result.metrics)
    return {ev.url for ev in result.evidence}, adapter.crawled, result.metrics


async def test_request_deny_drops_the_domain_before_the_crawl():
    kept, crawled, metrics = await _discover(
        CurationConstraints(domains_deny=["competitor.example"])
    )
    assert kept == {GOOD, OTHER}
    assert BAD not in crawled
    assert metrics["urls_dropped_policy"] == 1


async def test_request_allow_admits_only_its_domains():
    kept, crawled, _ = await _discover(
        CurationConstraints(domains_allow=["good.example"])
    )
    assert kept == {GOOD}
    assert BAD not in crawled and OTHER not in crawled


async def test_request_allow_narrows_the_policy_allow_list():
    kept, _, _ = await _discover(
        CurationConstraints(domains_allow=["good.example", "competitor.example"]),
        domains_allow=["good.example", "other.example"],
    )
    assert kept == {GOOD}


@pytest.mark.parametrize(
    ("constraints", "policy_kw"),
    [
        pytest.param(
            CurationConstraints(domains_allow=["good.example"]),
            {"domains_deny": ["good.example"]},
            id="policy-deny-beats-request-allow",
        ),
        pytest.param(
            CurationConstraints(domains_deny=["good.example"]),
            {"domains_allow": ["good.example"]},
            id="request-deny-beats-policy-allow",
        ),
    ],
)
async def test_a_deny_from_either_side_wins(constraints, policy_kw):
    kept, _, _ = await _discover(constraints, **policy_kw)
    assert GOOD not in kept


async def test_request_domains_match_at_label_boundaries():
    """Same host matching as the policy lists (B9): 'od.example' is not
    good.example, and a subdomain is covered by its parent."""
    kept, _, _ = await _discover(CurationConstraints(domains_deny=["od.example"]))
    assert kept == {BAD, GOOD, OTHER}
    kept, _, _ = await _discover(CurationConstraints(domains_allow=["od.example"]))
    assert kept == set()
    kept, _, _ = await _discover(CurationConstraints(domains_deny=["example"]))
    assert kept == set()


@pytest.mark.parametrize(
    ("constraints", "kept"),
    [
        pytest.param(
            CurationConstraints(date_from="2020-01-01"), {GOOD}, id="date-only-from"
        ),
        pytest.param(
            CurationConstraints(date_to="2020-01-01"), {BAD, OTHER}, id="date-only-to"
        ),
        pytest.param(
            CurationConstraints(date_from="2018-01-01T00:00:00", date_to="2020-01-01"),
            {OTHER},
            id="offset-less-range",
        ),
        pytest.param(
            CurationConstraints(date_from="2020-01-01T00:00:00Z"), {GOOD}, id="utc-z"
        ),
    ],
)
async def test_date_only_and_offset_less_bounds_filter(constraints, kept):
    got, _, metrics = await _discover(constraints)
    assert got == kept
    assert metrics["dropped_date"] == 3 - len(kept)


@pytest.mark.parametrize("value", ["last year", "2020-13-01", "yesterday"])
def test_unparseable_date_bound_is_rejected(value):
    with pytest.raises(ValidationError, match="ISO 8601"):
        CurationConstraints(date_from=value)
    with pytest.raises(ValidationError, match="ISO 8601"):
        CurationConstraints(date_to=value)


@pytest.mark.integration
@pytest.mark.parametrize(
    "constraints",
    [
        CurationConstraints(domains_deny=["competitor.example"]),
        CurationConstraints(domains_allow=["good.example", "other.example"]),
        CurationConstraints(date_from="2016-01-01"),
    ],
)
async def test_stored_rows_are_filtered_on_reuse_too(sqlite_store, constraints):
    """A URL stored by an earlier job is not reused past the request's
    constraints (the reuse split runs after the URL filter)."""
    discoverer = Discoverer(
        adapter=_adapter(), config=CrawlConfig(api_key="k"), evidence_store=sqlite_store
    )
    policy = make_source_policy(reputation=LAX)
    first = await discoverer.discover(make_curation_request(), policy)
    await sqlite_store.put_many(first.evidence)

    result = await discoverer.discover(
        make_curation_request(constraints=constraints), policy
    )

    assert {ev.url for ev in result.evidence} == {GOOD, OTHER}
    assert result.metrics["crawl_success"] == 0
    assert_ledgers_balance(result.metrics)


@pytest.mark.parametrize(
    "constraints",
    [None, CurationConstraints(), CurationConstraints(date_from="", date_to="")],
)
async def test_absent_or_empty_constraints_change_nothing(constraints):
    kept, crawled, metrics = await _discover(constraints)
    assert kept == {BAD, GOOD, OTHER}
    assert crawled == [BAD, GOOD, OTHER]
    assert metrics["urls_dropped_policy"] == 0
    assert metrics["dropped_date"] == 0
