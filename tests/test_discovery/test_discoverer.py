"""Tests for cce.discovery.discoverer — static methods and discovery pipeline."""

import hashlib

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlResult
from cce.discovery.discoverer import Discoverer
from cce.policy.types import RecencyRule, ReputationRule, SourcePolicy, TopicOverride
from tests.conftest import make_crawl_result, make_curation_request, make_evidence, make_source_policy

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
    policy = make_source_policy(
        domains_allow=["spam.com"], domains_deny=["spam.com"]
    )
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
        Discoverer._assess_reputation("https://mit.edu/paper", rules)
        == "institutional"
    )


def test_assess_reputation_unknown():
    rules = ReputationRule(trusted_institutions=[])
    assert (
        Discoverer._assess_reputation("https://randomsite.com/page", rules)
        == "unknown"
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
        make_evidence(id=f"ev_{i}", url=f"https://source{i}.com", excerpt=f"Excerpt {i} long enough.")
        for i in range(3)
    ]
    capped = Discoverer._cap_evidence(evidence, max_per_source=5, max_total=100)
    assert len(capped) == 3
    assert capped == evidence  # same list, unchanged


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


@pytest.mark.integration
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

    evidence = await discoverer.discover(request, policy)

    assert len(evidence) >= 2
    urls = {ev.url for ev in evidence}
    assert "https://example.com/article" in urls
    assert "https://example.org/study" in urls
    for ev in evidence:
        assert ev.excerpt_hash == hashlib.sha256(ev.excerpt.encode("utf-8")).hexdigest()
        assert ev.source_quality is not None


@pytest.mark.integration
async def test_discover_no_urls_after_filter():
    from tests.conftest import MockCrawlAdapter

    adapter = MockCrawlAdapter(
        search_map={"test topic": ["https://blocked.com/page"]},
        url_map={
            "https://blocked.com/page": make_crawl_result(url="https://blocked.com/page"),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="test"))
    request = make_curation_request(topic="test topic")
    policy = make_source_policy(domains_deny=["blocked.com"])

    evidence = await discoverer.discover(request, policy)
    assert evidence == []


@pytest.mark.integration
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

    evidence = await discoverer.discover(request, policy)
    assert evidence == []


@pytest.mark.integration
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

    evidence = await discoverer.discover(request, policy)
    # Only 2 sources should be crawled — each has extractable content, so exactly 2 URLs
    evidence_urls = {ev.url for ev in evidence}
    assert len(evidence_urls) == 2


@pytest.mark.integration
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

    evidence = await discoverer.discover(request, policy)
    # Same content → same hash → deduped to 1
    hashes = [ev.excerpt_hash for ev in evidence]
    assert len(hashes) == len(set(hashes))
