"""Tests for cce.discovery.discoverer — static methods and discovery pipeline."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlResult
from cce.discovery.discoverer import Discoverer
from cce.models.evidence import SourceQuality
from cce.models.request import CurationConstraints
from cce.policy.types import RecencyRule, ReputationRule, TopicOverride
from tests.conftest import (
    make_crawl_result,
    make_curation_request,
    make_evidence,
    make_source_policy,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _build_queries
# ---------------------------------------------------------------------------


def test_build_queries_topic_only():
    req = make_curation_request(topic="ai safety", subtopics=[])
    assert Discoverer._build_queries(req) == ["ai safety"]


def test_build_queries_with_subtopics():
    req = make_curation_request(
        topic="ai safety", subtopics=["alignment", "interpretability"]
    )
    assert Discoverer._build_queries(req) == [
        "ai safety",
        "ai safety alignment",
        "ai safety interpretability",
    ]


# ---------------------------------------------------------------------------
# _passes_policy
# ---------------------------------------------------------------------------


def test_passes_policy_allow_all():
    policy = make_source_policy(domains_allow=[], domains_deny=[])
    assert Discoverer._passes_policy("https://anything.com/page", policy) is True


def test_passes_policy_deny_blocks():
    policy = make_source_policy(domains_deny=["spam.com"])
    assert Discoverer._passes_policy("https://spam.com/page", policy) is False


def test_passes_policy_deny_takes_priority():
    policy = make_source_policy(domains_allow=["spam.com"], domains_deny=["spam.com"])
    assert Discoverer._passes_policy("https://spam.com/page", policy) is False


def test_passes_policy_allow_list_gates():
    policy = make_source_policy(domains_allow=["trusted.org"])
    assert Discoverer._passes_policy("https://other.com/page", policy) is False


def test_passes_policy_allow_list_passes():
    policy = make_source_policy(domains_allow=["trusted.org"])
    assert Discoverer._passes_policy("https://trusted.org/paper", policy) is True


def test_passes_policy_no_domain():
    policy = make_source_policy()
    assert Discoverer._passes_policy("not-a-url", policy) is False


def test_passes_policy_case_insensitive():
    policy = make_source_policy(domains_deny=["SPAM.COM"])
    assert Discoverer._passes_policy("https://spam.com/page", policy) is False


# ---------------------------------------------------------------------------
# _resolve_overrides
# ---------------------------------------------------------------------------


def test_resolve_overrides_no_match():
    policy = make_source_policy(
        topic_overrides=[
            TopicOverride(topic_pattern="medical", domains_allow=["nih.gov"])
        ]
    )
    result = Discoverer._resolve_overrides("cooking", policy)
    # No match — original policy returned unchanged
    assert result is policy


def test_resolve_overrides_match_merges():
    override_rep = ReputationRule(
        require_peer_reviewed=True,
        trusted_institutions=["nih.gov"],
    )
    policy = make_source_policy(
        domains_allow=["example.com"],
        domains_deny=["bad.com"],
        topic_overrides=[
            TopicOverride(
                topic_pattern="medical",
                domains_allow=["nih.gov"],
                domains_deny=["quack.com"],
                reputation=override_rep,
            )
        ],
    )
    result = Discoverer._resolve_overrides("medical research", policy)
    assert "example.com" in result.domains_allow
    assert "nih.gov" in result.domains_allow
    assert "bad.com" in result.domains_deny
    assert "quack.com" in result.domains_deny
    assert result.reputation.require_peer_reviewed is True
    assert "nih.gov" in result.reputation.trusted_institutions


def test_resolve_overrides_no_recursion():
    policy = make_source_policy(
        topic_overrides=[
            TopicOverride(topic_pattern="medical", domains_allow=["nih.gov"])
        ]
    )
    result = Discoverer._resolve_overrides("medical research", policy)
    assert result.topic_overrides == []


# ---------------------------------------------------------------------------
# _chunk_content
# ---------------------------------------------------------------------------


def test_chunk_content_empty():
    assert Discoverer._chunk_content("") == []


def test_chunk_content_single_paragraph():
    text = "This is a short paragraph."
    chunks = Discoverer._chunk_content(text)
    assert chunks == ["This is a short paragraph."]


def test_chunk_content_paragraph_split():
    text = "First paragraph here.\n\nSecond paragraph here."
    chunks = Discoverer._chunk_content(text)
    assert len(chunks) == 2
    assert chunks[0] == "First paragraph here."
    assert chunks[1] == "Second paragraph here."


def test_chunk_content_long_paragraph():
    # Single paragraph >1500 chars with embedded newlines
    lines = [f"Line {i}: " + "x" * 100 for i in range(20)]
    text = "\n".join(lines)  # ~2200 chars total, single paragraph (no double newlines)
    chunks = Discoverer._chunk_content(text, max_chunk_size=1500)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 1500


# ---------------------------------------------------------------------------
# Quality heuristics
# ---------------------------------------------------------------------------


def test_looks_peer_reviewed_doi():
    cr = make_crawl_result(url="https://doi.org/10.1234/test")
    assert Discoverer._looks_peer_reviewed(cr) is True


def test_looks_peer_reviewed_pubmed():
    cr = make_crawl_result(url="https://pubmed.ncbi.nlm.nih.gov/12345")
    assert Discoverer._looks_peer_reviewed(cr) is True


def test_looks_peer_reviewed_normal():
    cr = make_crawl_result(url="https://blog.com/my-post")
    assert Discoverer._looks_peer_reviewed(cr) is False


def test_looks_primary_gov_edu_org():
    for suffix in [".gov", ".edu", ".org"]:
        cr = make_crawl_result(url=f"https://example{suffix}/page")
        assert Discoverer._looks_primary(cr) is True, f"Failed for {suffix}"


def test_looks_primary_com():
    cr = make_crawl_result(url="https://example.com/page")
    assert Discoverer._looks_primary(cr) is False


def test_assess_reputation_trusted():
    rules = ReputationRule(trusted_institutions=["nih.gov"])
    assert Discoverer._assess_reputation("https://nih.gov/study", rules) == "trusted"


def test_assess_reputation_institutional():
    rules = ReputationRule(trusted_institutions=[])
    assert (
        Discoverer._assess_reputation("https://mit.edu/paper", rules) == "institutional"
    )


def test_assess_reputation_unknown():
    rules = ReputationRule(trusted_institutions=[])
    assert (
        Discoverer._assess_reputation("https://randomsite.com/page", rules) == "unknown"
    )


def test_looks_marketing_positive():
    cr = make_crawl_result(
        markdown="Check out this amazing deal! Buy now and save 50%!",
        title="Special Offer",
    )
    assert Discoverer._looks_marketing(cr) is True


def test_looks_marketing_negative():
    cr = make_crawl_result(
        markdown="This peer-reviewed study examines the effects of gene therapy.",
        title="Research Paper",
    )
    assert Discoverer._looks_marketing(cr) is False


# ---------------------------------------------------------------------------
# _passes_date_filter
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 3, 24, tzinfo=UTC)


def test_passes_date_filter_no_published_at_passes():
    """Fail-open: evidence with no published_at always passes."""
    ev = make_evidence(published_at=None, retrieved_at=_NOW)
    policy = make_source_policy(recency=RecencyRule(max_age_days=30))
    assert Discoverer._passes_date_filter(ev, policy, None) is True


def test_passes_date_filter_within_max_age_passes():
    ev = make_evidence(
        published_at=_NOW - timedelta(days=10),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=30))
    assert Discoverer._passes_date_filter(ev, policy, None) is True


def test_passes_date_filter_exceeds_max_age_fails():
    ev = make_evidence(
        published_at=_NOW - timedelta(days=60),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=30))
    assert Discoverer._passes_date_filter(ev, policy, None) is False


def test_passes_date_filter_no_max_age_passes():
    """max_age_days=None means no age limit."""
    ev = make_evidence(
        published_at=_NOW - timedelta(days=9999),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=None))
    assert Discoverer._passes_date_filter(ev, policy, None) is True


def test_passes_date_filter_date_from_constraint():
    ev = make_evidence(
        published_at=datetime(2022, 6, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=None))
    constraints = CurationConstraints(date_from="2023-01-01T00:00:00+00:00")
    assert Discoverer._passes_date_filter(ev, policy, constraints) is False

    ev_recent = make_evidence(
        published_at=datetime(2023, 6, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    assert Discoverer._passes_date_filter(ev_recent, policy, constraints) is True


def test_passes_date_filter_date_to_constraint():
    ev = make_evidence(
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=None))
    constraints = CurationConstraints(date_to="2025-01-01T00:00:00+00:00")
    assert Discoverer._passes_date_filter(ev, policy, constraints) is False

    ev_older = make_evidence(
        published_at=datetime(2024, 6, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    assert Discoverer._passes_date_filter(ev_older, policy, constraints) is True


def test_passes_date_filter_composes_max_age_and_constraints():
    """The tighter bound wins: max_age_days=365 vs date_from=2025-09-01."""
    ev = make_evidence(
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    # max_age_days=365 → cutoff ~2025-03-24, so 2025-06-01 passes
    # date_from=2025-09-01 → 2025-06-01 fails
    policy = make_source_policy(recency=RecencyRule(max_age_days=365))
    constraints = CurationConstraints(date_from="2025-09-01T00:00:00+00:00")
    assert Discoverer._passes_date_filter(ev, policy, constraints) is False


def test_passes_date_filter_naive_constraint_passes():
    """Fail-open when constraint date has no timezone (naive vs aware comparison)."""
    ev = make_evidence(
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=None))
    # Bare date with no timezone → naive datetime → TypeError on comparison → fail-open
    constraints = CurationConstraints(date_from="2025-01-01")
    assert Discoverer._passes_date_filter(ev, policy, constraints) is True


def test_passes_date_filter_naive_published_at_with_max_age_passes():
    """Fail-open when published_at is naive but retrieved_at is aware (max_age_days check)."""
    ev = make_evidence(
        published_at=datetime(2024, 1, 1),  # naive — no tzinfo
        retrieved_at=_NOW,  # aware — has timezone.utc
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=30))
    assert Discoverer._passes_date_filter(ev, policy, None) is True


def test_passes_date_filter_invalid_constraint_passes():
    """Fail-open on unparseable constraint dates."""
    ev = make_evidence(
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        retrieved_at=_NOW,
    )
    policy = make_source_policy(recency=RecencyRule(max_age_days=None))
    constraints = CurationConstraints(date_from="not-a-date", date_to="also-bad")
    assert Discoverer._passes_date_filter(ev, policy, constraints) is True


# ---------------------------------------------------------------------------
# _passes_reputation_filter
# ---------------------------------------------------------------------------


def test_passes_reputation_filter_no_source_quality_passes():
    """Fail-open: evidence with no source_quality always passes."""
    ev = make_evidence(source_quality=None)
    rules = ReputationRule(require_peer_reviewed=True)
    assert Discoverer._passes_reputation_filter(ev, rules) is True


def test_passes_reputation_filter_require_peer_reviewed_blocks():
    ev = make_evidence(
        source_quality=SourceQuality(is_peer_reviewed=False),
    )
    rules = ReputationRule(require_peer_reviewed=True)
    assert Discoverer._passes_reputation_filter(ev, rules) is False


def test_passes_reputation_filter_require_peer_reviewed_passes():
    ev = make_evidence(
        source_quality=SourceQuality(is_peer_reviewed=True),
    )
    rules = ReputationRule(require_peer_reviewed=True)
    assert Discoverer._passes_reputation_filter(ev, rules) is True


def test_passes_reputation_filter_require_primary_blocks():
    ev = make_evidence(
        source_quality=SourceQuality(is_primary_source=False),
    )
    rules = ReputationRule(require_primary_source=True)
    assert Discoverer._passes_reputation_filter(ev, rules) is False


def test_passes_reputation_filter_block_marketing_blocks():
    ev = make_evidence(
        source_quality=SourceQuality(conflict_of_interest=True),
    )
    rules = ReputationRule(block_marketing=True)
    assert Discoverer._passes_reputation_filter(ev, rules) is False


def test_passes_reputation_filter_defaults_allow_clean():
    """Default policy (require_*=False, block_marketing=True) passes clean evidence."""
    ev = make_evidence(
        source_quality=SourceQuality(
            is_peer_reviewed=False,
            is_primary_source=False,
            conflict_of_interest=False,
        ),
    )
    rules = ReputationRule()  # defaults
    assert Discoverer._passes_reputation_filter(ev, rules) is True


# ---------------------------------------------------------------------------
# _cap_evidence
# ---------------------------------------------------------------------------


def test_cap_evidence_per_source():
    # 3 sources × 10 excerpts each, max_per_source=5 → 15 total, longest kept
    evidence = []
    for src in range(3):
        for i in range(10):
            evidence.append(
                make_evidence(
                    id=f"ev_s{src}_{i}",
                    url=f"https://source{src}.com/page",
                    excerpt="x" * (100 + i * 10),  # increasing length
                )
            )
    capped = Discoverer._cap_evidence(evidence, max_per_source=5, max_total=1000)
    assert len(capped) == 15  # 3 × 5
    # Each source's longest 5 should be kept
    for src in range(3):
        src_evidence = [e for e in capped if e.url == f"https://source{src}.com/page"]
        assert len(src_evidence) == 5
        # Verify longest were kept (shortest excerpt in group should be ≥ 150 chars)
        assert all(len(e.excerpt) >= 150 for e in src_evidence)


def test_cap_evidence_global():
    # 20 sources × 5 excerpts = 100, max_total=50 → exactly 50
    evidence = []
    for src in range(20):
        for i in range(5):
            evidence.append(
                make_evidence(
                    id=f"ev_s{src}_{i}",
                    url=f"https://source{src}.com/page",
                    excerpt="x" * (100 + i * 10),
                )
            )
    capped = Discoverer._cap_evidence(evidence, max_per_source=5, max_total=50)
    assert len(capped) == 50


def test_cap_evidence_no_op():
    # Already under both caps → returned unchanged
    evidence = [
        make_evidence(
            id=f"ev_{i}",
            url=f"https://source{i}.com",
            excerpt=f"Excerpt {i} long enough.",
        )
        for i in range(3)
    ]
    capped = Discoverer._cap_evidence(evidence, max_per_source=5, max_total=100)
    assert len(capped) == 3
    assert capped == evidence  # same list, unchanged


def test_cap_evidence_prefer_recent_sorts_by_date():
    """When prefer_recent=True, newer evidence wins over longer excerpts."""
    old_long = make_evidence(
        id="ev_old",
        url="https://source.com/page",
        excerpt="x" * 500,
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    new_short = make_evidence(
        id="ev_new",
        url="https://source.com/page",
        excerpt="x" * 100,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    capped = Discoverer._cap_evidence(
        [old_long, new_short], max_per_source=1, max_total=10, prefer_recent=True
    )
    assert len(capped) == 1
    assert capped[0].id == "ev_new"


def test_cap_evidence_prefer_recent_false_preserves_length():
    """When prefer_recent=False, longer excerpts still win (existing behavior)."""
    old_long = make_evidence(
        id="ev_old",
        url="https://source.com/page",
        excerpt="x" * 500,
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    new_short = make_evidence(
        id="ev_new",
        url="https://source.com/page",
        excerpt="x" * 100,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    capped = Discoverer._cap_evidence(
        [old_long, new_short], max_per_source=1, max_total=10, prefer_recent=False
    )
    assert len(capped) == 1
    assert capped[0].id == "ev_old"


def test_cap_evidence_prefer_recent_missing_dates_sorted_last():
    """Evidence with no published_at sorts after dated evidence when prefer_recent=True."""
    dated = make_evidence(
        id="ev_dated",
        url="https://source.com/page",
        excerpt="x" * 100,
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    undated = make_evidence(
        id="ev_undated",
        url="https://source.com/page",
        excerpt="x" * 500,  # longer but no date
        published_at=None,
    )
    capped = Discoverer._cap_evidence(
        [undated, dated], max_per_source=1, max_total=10, prefer_recent=True
    )
    assert len(capped) == 1
    assert capped[0].id == "ev_dated"


# ---------------------------------------------------------------------------
# _extract_evidence
# ---------------------------------------------------------------------------


def test_extract_evidence_creates_objects():
    cr = make_crawl_result(
        url="https://example.org/article",
        title="Test Title",
        author="Author A",
        published_date="2024-06-01T00:00:00Z",
        markdown=(
            "First paragraph with enough content to exceed the fifty character minimum.\n\n"
            "Second paragraph also long enough to be extracted as evidence by the discoverer."
        ),
    )

    discoverer = Discoverer(
        adapter=None,  # type: ignore[arg-type]  # not used by _extract_evidence
        config=CrawlConfig(api_key="test"),
    )
    policy = make_source_policy()
    evidence = discoverer._extract_evidence(cr, policy)

    assert len(evidence) == 2
    for ev in evidence:
        assert ev.url == "https://example.org/article"
        assert ev.title == "Test Title"
        assert ev.author == "Author A"
        assert ev.id.startswith("ev_")
        assert ev.excerpt_hash == hashlib.sha256(ev.excerpt.encode("utf-8")).hexdigest()
        assert ev.locator.startswith("chunk:")
        assert ev.source_quality is not None


def test_extract_evidence_published_at_parsing():

    discoverer = Discoverer(
        adapter=None,  # type: ignore[arg-type]
        config=CrawlConfig(api_key="test"),
    )
    policy = make_source_policy()

    # Valid ISO date
    cr_valid = make_crawl_result(
        published_date="2024-01-15T00:00:00Z",
        markdown="A long enough paragraph to pass the fifty character minimum for extraction.",
    )
    evidence = discoverer._extract_evidence(cr_valid, policy)
    assert len(evidence) >= 1
    assert evidence[0].published_at is not None
    assert evidence[0].published_at.year == 2024

    # Invalid date
    cr_invalid = make_crawl_result(
        published_date="not-a-date",
        markdown="Another long enough paragraph to pass the fifty character minimum for extraction.",
    )
    evidence = discoverer._extract_evidence(cr_invalid, policy)
    assert len(evidence) >= 1
    assert evidence[0].published_at is None


# ===========================================================================
# Integration tests (async, mocked adapter)
# ===========================================================================


async def test_discover_full_flow():
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={
            "test topic": [
                "https://example.com/article",
                "https://example.org/study",
            ],
        },
        url_map={
            "https://example.com/article": make_crawl_result(
                url="https://example.com/article",
                title="Article One",
                markdown="This is the first article with enough content to extract as evidence for the test.",
            ),
            "https://example.org/study": make_crawl_result(
                url="https://example.org/study",
                title="Study Two",
                markdown="This is the second study with plenty of content to pass the fifty char minimum.",
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    evidence = (await discoverer.discover(request, policy)).evidence

    assert len(evidence) >= 2
    urls = {ev.url for ev in evidence}
    assert "https://example.com/article" in urls
    assert "https://example.org/study" in urls
    for ev in evidence:
        assert ev.excerpt_hash == hashlib.sha256(ev.excerpt.encode("utf-8")).hexdigest()
        assert ev.source_quality is not None


async def test_discover_no_urls_after_filter():
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://blocked.com/page"]},
        url_map={
            "https://blocked.com/page": make_crawl_result(
                url="https://blocked.com/page"
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy(domains_deny=["blocked.com"])

    evidence = (await discoverer.discover(request, policy)).evidence
    assert evidence == []


async def test_discover_empty_crawl_skipped():
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://empty.com/page"]},
        url_map={
            "https://empty.com/page": CrawlResult(
                url="https://empty.com/page", status_code=0, markdown=""
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    evidence = (await discoverer.discover(request, policy)).evidence
    assert evidence == []


async def test_discover_max_sources_cap():
    from tests.conftest import MockCrawlAdapter

    urls = [f"https://site{i}.com/page" for i in range(10)]
    url_map = {
        url: make_crawl_result(
            url=url,
            markdown=f"Content from site {i} with enough words to pass the minimum length check.",
        )
        for i, url in enumerate(urls)
    }
    adapter = MockCrawlAdapter(search_map={"test topic": urls}, url_map=url_map)
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy(max_sources_per_run=2)

    evidence = (await discoverer.discover(request, policy)).evidence
    # Only 2 sources should be crawled — each has extractable content, so exactly 2 URLs
    evidence_urls = {ev.url for ev in evidence}
    assert len(evidence_urls) == 2


async def test_discover_dedup_by_hash():
    from tests.conftest import MockCrawlAdapter

    shared_markdown = "This identical paragraph appears on two different sites and should be deduped by hash."
    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://a.com/page", "https://b.com/page"]},
        url_map={
            "https://a.com/page": make_crawl_result(
                url="https://a.com/page", markdown=shared_markdown
            ),
            "https://b.com/page": make_crawl_result(
                url="https://b.com/page", markdown=shared_markdown
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    evidence = (await discoverer.discover(request, policy)).evidence
    # Same content → same hash → deduped to 1
    hashes = [ev.excerpt_hash for ev in evidence]
    assert len(hashes) == len(set(hashes))


async def test_discover_filters_old_and_marketing_evidence():
    """Evidence that is too old or marketing-flagged is filtered out post-extraction."""
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={
            "test topic": [
                "https://good.org/page",
                "https://old.org/page",
                "https://spammy.com/page",
            ],
        },
        url_map={
            "https://good.org/page": make_crawl_result(
                url="https://good.org/page",
                published_date="2026-01-01T00:00:00Z",
                markdown="Good recent content with enough words to pass the minimum length check.",
            ),
            "https://old.org/page": make_crawl_result(
                url="https://old.org/page",
                published_date="2010-01-01T00:00:00Z",
                markdown="Very old content that should be filtered by the max age days policy.",
            ),
            "https://spammy.com/page": make_crawl_result(
                url="https://spammy.com/page",
                published_date="2026-01-01T00:00:00Z",
                markdown="Buy now! Limited time offer on this amazing sponsored product deal!",
                title="Sponsored Ad",
            ),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy(
        recency=RecencyRule(max_age_days=365),
        reputation=ReputationRule(block_marketing=True),
    )

    evidence = (await discoverer.discover(request, policy)).evidence

    urls = {ev.url for ev in evidence}
    assert "https://good.org/page" in urls
    assert "https://old.org/page" not in urls
    assert "https://spammy.com/page" not in urls


# ===========================================================================
# Embedding relevance ranking
# ===========================================================================


# ---------------------------------------------------------------------------
# _cap_sort_key with relevance
# ---------------------------------------------------------------------------


def test_cap_sort_key_relevance_beats_recency():
    """High relevance + old date should beat low relevance + new date."""
    old_ev = make_evidence(
        id="ev_old",
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
        excerpt="x" * 100,
    )
    new_ev = make_evidence(
        id="ev_new",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        excerpt="x" * 100,
    )
    scores = {"ev_old": 0.95, "ev_new": 0.3}
    key_old = Discoverer._cap_sort_key(
        old_ev, prefer_recent=True, relevance_scores=scores
    )
    key_new = Discoverer._cap_sort_key(
        new_ev, prefer_recent=True, relevance_scores=scores
    )
    # old_ev has higher relevance → should sort higher
    assert key_old > key_new


def test_cap_sort_key_without_relevance_matches_old_behavior():
    """When relevance_scores=None, relevance is 0.0 — recency and length decide."""
    ev = make_evidence(
        id="ev_1",
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
        excerpt="x" * 200,
    )
    key = Discoverer._cap_sort_key(ev, prefer_recent=True, relevance_scores=None)
    assert key[0] == 0.0  # relevance
    assert key[1] > 0  # recency (timestamp)
    assert key[2] == 200  # length


# ---------------------------------------------------------------------------
# _cap_evidence with relevance_scores
# ---------------------------------------------------------------------------


def test_cap_evidence_with_relevance_scores():
    """Top relevance evidence should be kept over longer but less relevant."""
    evidence = [
        make_evidence(
            id=f"ev_{i}", url="https://source.com/page", excerpt="x" * (100 + i * 50)
        )
        for i in range(5)
    ]
    # Give the shortest excerpt the highest relevance
    scores = {f"ev_{i}": float(4 - i) / 4.0 for i in range(5)}
    capped = Discoverer._cap_evidence(
        evidence, max_per_source=2, max_total=10, relevance_scores=scores
    )
    assert len(capped) == 2
    # ev_0 has highest relevance (1.0), ev_1 has second (0.75)
    assert capped[0].id == "ev_0"
    assert capped[1].id == "ev_1"


def test_cap_evidence_relevance_scores_none_backward_compat():
    """relevance_scores=None preserves existing length-based behavior."""
    short = make_evidence(id="ev_short", url="https://s.com/p", excerpt="x" * 50)
    long = make_evidence(id="ev_long", url="https://s.com/p", excerpt="x" * 500)
    capped = Discoverer._cap_evidence(
        [short, long], max_per_source=1, max_total=10, relevance_scores=None
    )
    assert len(capped) == 1
    assert capped[0].id == "ev_long"


def test_cap_evidence_empty_relevance_scores_degrades():
    """Empty dict should degrade to length-based (all relevance = 0.0)."""
    short = make_evidence(id="ev_short", url="https://s.com/p", excerpt="x" * 50)
    long = make_evidence(id="ev_long", url="https://s.com/p", excerpt="x" * 500)
    capped = Discoverer._cap_evidence(
        [short, long], max_per_source=1, max_total=10, relevance_scores={}
    )
    assert len(capped) == 1
    assert capped[0].id == "ev_long"


# ---------------------------------------------------------------------------
# discover() with embedding provider
# ---------------------------------------------------------------------------


async def test_discover_with_embedding_provider():
    """discover() calls embed and uses relevance scores for ranking."""
    from tests.conftest import MockCrawlAdapter, MockEmbeddingProvider

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://example.com/page"]},
        url_map={
            "https://example.com/page": make_crawl_result(
                url="https://example.com/page",
                markdown="Content about the test topic with enough words for extraction purposes.",
            ),
        },
    )
    embedding = MockEmbeddingProvider(dimension=8)
    discoverer = Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test"),
        embedding_provider=embedding,
    )
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    evidence = (await discoverer.discover(request, policy)).evidence

    assert len(evidence) >= 1
    # Embedding provider should have been called once
    assert len(embedding.calls) == 1
    # First text in the call should be the query (topic)
    assert embedding.calls[0][0] == "test topic"


async def test_discover_embedding_fallback_on_failure():
    """discover() falls back to length-based ranking when embeddings fail."""
    from tests.conftest import MockCrawlAdapter, MockEmbeddingProvider

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://example.com/page"]},
        url_map={
            "https://example.com/page": make_crawl_result(
                url="https://example.com/page",
                markdown="Content about the test topic with enough words for extraction purposes.",
            ),
        },
    )
    embedding = MockEmbeddingProvider(fail=True)
    discoverer = Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test"),
        embedding_provider=embedding,
    )
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    # Should not raise — falls back gracefully
    evidence = (await discoverer.discover(request, policy)).evidence
    assert len(evidence) >= 1


async def test_discover_embedding_failure_warning_names_ollama_and_env_flag(caplog):
    """PDR-002 (T-07.01): the fallback warning must tell the operator what
    was down (Ollama + base URL when known) and how to silence the fallback
    deliberately (CCE_EMBEDDING_ENABLED=false)."""
    import logging

    from tests.conftest import MockCrawlAdapter, MockEmbeddingProvider

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://example.com/page"]},
        url_map={
            "https://example.com/page": make_crawl_result(
                url="https://example.com/page",
                markdown="Content about the test topic with enough words for extraction purposes.",
            ),
        },
    )
    discoverer = Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test"),
        embedding_provider=MockEmbeddingProvider(fail=True),
    )
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    with caplog.at_level(logging.WARNING, logger="cce.discovery.discoverer"):
        result = await discoverer.discover(request, policy)

    assert len(result.evidence) >= 1  # length-based fallback still ran
    warning = next(
        r.getMessage()
        for r in caplog.records
        if "Embedding unavailable" in r.getMessage()
    )
    assert "Ollama" in warning
    assert "CCE_EMBEDDING_ENABLED=false" in warning


async def test_discover_no_embedding_provider():
    """discover() with embedding_provider=None behaves identically to before."""
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://example.com/page"]},
        url_map={
            "https://example.com/page": make_crawl_result(
                url="https://example.com/page",
                markdown="Content about the test topic with enough words for extraction purposes.",
            ),
        },
    )
    discoverer = Discoverer(
        adapter=adapter,
        config=CrawlConfig(api_key="test"),
        embedding_provider=None,
    )
    request = make_curation_request(topic="test topic")
    policy = make_source_policy()

    evidence = (await discoverer.discover(request, policy)).evidence
    assert len(evidence) >= 1
