"""CR-11: peer-review and trusted-source tags are earned by the host only.

They were matched as substrings of the whole URL (peer review) or of the
netloc (trusted institutions), so a path, query or userinfo chosen by
whoever publishes the page could earn them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import Discoverer
from cce.policy.types import ReputationRule, SourcePolicy
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _peer(url: str) -> bool:
    return Discoverer._looks_peer_reviewed(make_crawl_result(url=url))


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/blog/pubmed-roundup",
        "https://attacker.example/post?utm_source=doi.org",
        "https://attacker.example/arxiv.org-mirror",
        "https://attacker.example/p#ncbi.nlm.nih.gov",
        "https://doi.org@attacker.example/x",
        "https://notarxiv.org/abs/1",
        "https://arxiv.org.attacker.example/abs/1",
    ],
)
def test_page_controlled_parts_do_not_earn_peer_review(url):
    assert _peer(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.1234/test",
        "https://dx.doi.org/10.1234/test",
        "https://pubmed.ncbi.nlm.nih.gov/12345",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1/",
        "https://arxiv.org/abs/2401.00001",
        "https://export.arxiv.org/abs/2401.00001",
        "https://scholar.google.com/scholar?q=x",
        "https://PubMed.ncbi.nlm.nih.gov:443/1",
    ],
)
def test_real_hosts_keep_peer_review(url):
    assert _peer(url) is True


@pytest.mark.parametrize(
    ("url", "trusted", "tier"),
    [
        # userinfo, path and query are not the host
        ("https://nih.gov@attacker.example/x", ["nih.gov"], "unknown"),
        ("https://attacker.example/nih.gov/x", ["nih.gov"], "unknown"),
        ("https://attacker.example/x?pubmed=1", ["pubmed"], "unknown"),
        # label boundaries: a lookalike or a trailing label is not the host
        ("https://nih.gov.attacker.example/x", ["nih.gov"], "unknown"),
        ("https://fakenih.gov.example/x", ["nih.gov"], "unknown"),
        # the host or a subdomain still earns it, and a port is ignored
        ("https://nih.gov/study", ["nih.gov"], "trusted"),
        ("https://www.nih.gov:443/study", ["nih.gov"], "trusted"),
        ("https://www.mit.edu/x", [".edu"], "trusted"),
        # deliberate: '.gov' is the gov TLD, as for primary_source_suffixes
        # (B9); a ccTLD second level such as gov.uk must be listed itself
        ("https://www.gov.uk/x", [".gov"], "unknown"),
        ("https://www.gov.uk/x", ["gov.uk"], "trusted"),
        # a bare entry still matches a host label (shipped policies use it)
        ("https://pubmed.ncbi.nlm.nih.gov/1", ["pubmed"], "trusted"),
        ("https://pubmedcentral.example/1", ["pubmed"], "unknown"),
        # institutional tier from the host, port ignored
        ("https://www.nih.gov:443/x", [], "institutional"),
        ("https://mit.edu@attacker.example/x", [], "unknown"),
    ],
)
def test_reputation_tier_is_read_from_the_host(url, trusted, tier):
    rules = ReputationRule(trusted_institutions=trusted)
    assert Discoverer._assess_reputation(url, rules) == tier


def _shipped_trusted_entries() -> set[str]:
    entries: set[str] = set()
    files = sorted((REPO / "policies").glob("*.yaml")) + sorted(
        (REPO / "policies" / "examples").glob("*.yaml")
    )
    for f in files:
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict) and "id" in data and "name" in data:
            policy = SourcePolicy(**data)
            entries.update(policy.reputation.trusted_institutions)
    return entries


def test_shipped_trusted_entries_still_match_their_hosts():
    """Every trusted_institutions entry of a shipped policy still marks the
    host it names, and its subdomains, as trusted."""
    entries = _shipped_trusted_entries()
    assert {"pubmed", "nih.gov", ".gov"} <= entries
    for entry in entries:
        rules = ReputationRule(trusted_institutions=[entry])
        name = entry.lstrip(".")
        if "." in entry:
            hosts = [f"example.{name}"] if entry.startswith(".") else [name]
        else:
            hosts = [f"{name}.ncbi.nlm.nih.gov"]
        for host in hosts + [f"www.{h}" for h in hosts]:
            url = f"https://{host}/page"
            assert Discoverer._assess_reputation(url, rules) == "trusted", url


async def test_require_peer_reviewed_drops_a_forged_path_end_to_end():
    forged = "https://attacker.example/blog/pubmed-roundup"
    real = "https://pubmed.ncbi.nlm.nih.gov/12345"
    adapter = MockCrawlAdapter(
        search_map={"test topic": [forged, real]},
        url_map={
            forged: make_crawl_result(url=forged, markdown="Forged page " * 10),
            real: make_crawl_result(url=real, markdown="Real abstract " * 10),
        },
    )
    policy = make_source_policy(
        reputation=ReputationRule(require_peer_reviewed=True, trusted_institutions=[])
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))

    result = await discoverer.discover(make_curation_request(), policy)

    assert {ev.url for ev in result.evidence} == {real}
    assert result.metrics["dropped_reputation"] == 1
