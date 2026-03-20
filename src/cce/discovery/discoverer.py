"""Source discoverer.

Owns the full discover + extract + normalize step. Takes a CurationRequest
and SourcePolicy, uses a CrawlAdapter to fetch pages, and produces
Evidence objects ready for the evidence store.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult
from cce.models.evidence import Evidence, SourceQuality
from cce.models.request import CurationRequest
from cce.policy.types import RecencyRule, ReputationRule, SourcePolicy, TopicOverride

logger = logging.getLogger(__name__)


class Discoverer:
    """Discovers sources, applies policy filters, extracts evidence."""

    def __init__(
        self,
        adapter: CrawlAdapter,
        config: CrawlConfig,
    ) -> None:
        self._adapter = adapter
        self._config = config

    async def discover(
        self,
        request: CurationRequest,
        policy: SourcePolicy,
    ) -> list[Evidence]:
        """Run the full discovery pipeline for a curation request.

        1. Build search queries from the request topic + subtopics
        2. Search for URLs (via adapter) or use constraint-provided seed URLs
        3. Filter URLs against the source policy
        4. Crawl the filtered URLs
        5. Extract and normalize into Evidence objects
        """
        # Step 1: Build search queries
        queries = self._build_queries(request)
        logger.info("Discovery: %d search queries for topic '%s'", len(queries), request.topic)

        # Step 2: Search for candidate URLs
        candidate_urls: list[str] = []
        for query in queries:
            try:
                urls = await self._adapter.search(query, limit=20)
                candidate_urls.extend(urls)
            except NotImplementedError:
                logger.info("Adapter does not support search, skipping query: %s", query)

        # Add any seed domains from constraints as fallback
        if request.constraints and request.constraints.domains_allow:
            for domain in request.constraints.domains_allow:
                candidate_urls.append(f"https://{domain}")

        # Deduplicate
        candidate_urls = list(dict.fromkeys(candidate_urls))
        logger.info("Discovery: %d candidate URLs before policy filter", len(candidate_urls))

        # Step 3: Filter against policy
        effective_policy = self._resolve_overrides(request.topic, policy)
        filtered_urls = [
            url for url in candidate_urls if self._passes_policy(url, effective_policy)
        ]

        # Cap at max sources
        filtered_urls = filtered_urls[: policy.max_sources_per_run]
        logger.info("Discovery: %d URLs after policy filter", len(filtered_urls))

        if not filtered_urls:
            logger.warning("Discovery: no URLs survived policy filter")
            return []

        # Step 4: Crawl
        crawl_requests = [
            CrawlRequest(
                url=url,
                timeout_seconds=self._config.timeout_seconds,
            )
            for url in filtered_urls
        ]
        crawl_results = await self._adapter.crawl_many(crawl_requests)

        # Step 5: Extract and normalize (with in-run dedup by excerpt hash)
        evidence: list[Evidence] = []
        seen_hashes: set[str] = set()
        for result in crawl_results:
            if result.status_code == 0 or not result.markdown.strip():
                logger.debug("Skipping empty or failed crawl: %s", result.url)
                continue

            extracted = self._extract_evidence(result, effective_policy)
            for ev in extracted:
                if ev.excerpt_hash not in seen_hashes:
                    seen_hashes.add(ev.excerpt_hash)
                    evidence.append(ev)

        # Step 6: Cap evidence volume
        before_cap = len(evidence)
        evidence = self._cap_evidence(
            evidence,
            max_per_source=self._config.max_excerpts_per_source,
            max_total=self._config.max_evidence_total,
        )

        logger.info(
            "Discovery complete: %d evidence objects from %d pages (%d before cap)",
            len(evidence),
            len(crawl_results),
            before_cap,
        )
        return evidence

    # -- Query building --

    @staticmethod
    def _build_queries(request: CurationRequest) -> list[str]:
        """Build search queries from the request."""
        queries = [request.topic]
        for sub in request.subtopics:
            queries.append(f"{request.topic} {sub}")
        return queries

    # -- Policy resolution --

    @staticmethod
    def _resolve_overrides(topic: str, policy: SourcePolicy) -> SourcePolicy:
        """Apply any matching topic overrides to the base policy."""
        for override in policy.topic_overrides:
            if re.search(override.topic_pattern, topic, re.IGNORECASE):
                # Layer override fields onto a copy of the base policy
                merged_allow = policy.domains_allow + override.domains_allow
                merged_deny = policy.domains_deny + override.domains_deny
                return SourcePolicy(
                    id=policy.id,
                    name=policy.name,
                    domains_allow=merged_allow,
                    domains_deny=merged_deny,
                    reputation=override.reputation or policy.reputation,
                    recency=override.recency or policy.recency,
                    max_sources_per_run=policy.max_sources_per_run,
                    topic_overrides=[],  # don't recurse
                )
        return policy

    @staticmethod
    def _passes_policy(url: str, policy: SourcePolicy) -> bool:
        """Check if a URL is allowed by the source policy."""
        domain = urlparse(url).netloc.lower()
        if not domain:
            return False

        # Deny list takes priority
        for denied in policy.domains_deny:
            if denied.lower() in domain:
                return False

        # If allow list is non-empty, URL must match
        if policy.domains_allow:
            matched = any(
                allowed.lower() in domain for allowed in policy.domains_allow
            )
            if not matched:
                return False

        return True

    # -- Extraction --

    def _extract_evidence(
        self, result: CrawlResult, policy: SourcePolicy
    ) -> list[Evidence]:
        """Extract evidence objects from a crawl result.

        Splits the page content into meaningful chunks (by paragraph or
        section) and creates one Evidence object per chunk. Each chunk
        is a verbatim excerpt with full provenance.
        """
        chunks = self._chunk_content(result.markdown)
        now = datetime.now(timezone.utc)

        quality = SourceQuality(
            is_peer_reviewed=self._looks_peer_reviewed(result),
            is_primary_source=self._looks_primary(result),
            domain_reputation=self._assess_reputation(result.url, policy.reputation),
            conflict_of_interest=self._looks_marketing(result),
        )

        evidence: list[Evidence] = []
        for i, chunk in enumerate(chunks):
            text = chunk.strip()
            if len(text) < 50:  # skip tiny fragments
                continue

            excerpt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            published_at = None
            if result.published_date:
                try:
                    published_at = datetime.fromisoformat(
                        result.published_date.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            # Coerce metadata fields — adapters may return lists instead of strings
            title = result.title if isinstance(result.title, str) else ", ".join(result.title) if result.title else None
            author = result.author if isinstance(result.author, str) else ", ".join(result.author) if result.author else None

            evidence.append(
                Evidence(
                    id=f"ev_{uuid.uuid4().hex[:12]}",
                    url=result.url,
                    title=title or None,
                    author=author or None,
                    published_at=published_at,
                    retrieved_at=now,
                    excerpt=text,
                    excerpt_hash=excerpt_hash,
                    locator=f"chunk:{i}",
                    source_quality=quality,
                )
            )

        return evidence

    @staticmethod
    def _chunk_content(markdown: str, max_chunk_size: int = 1500) -> list[str]:
        """Split markdown into chunks, preferring section/paragraph boundaries.

        Strategy: split on double newlines (paragraph breaks) first. If a
        chunk exceeds max_chunk_size, split it further on single newlines.
        """
        if not markdown:
            return []

        paragraphs = re.split(r"\n\n+", markdown)
        chunks: list[str] = []

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(para) <= max_chunk_size:
                chunks.append(para)
            else:
                # Split long paragraphs on single newlines
                lines = para.split("\n")
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 > max_chunk_size and current:
                        chunks.append(current.strip())
                        current = line
                    else:
                        current = f"{current}\n{line}" if current else line
                if current.strip():
                    chunks.append(current.strip())

        return chunks

    # -- Evidence capping --

    @staticmethod
    def _cap_evidence(
        evidence: list[Evidence],
        max_per_source: int,
        max_total: int,
    ) -> list[Evidence]:
        """Cap evidence volume with per-source and global limits.

        Per-source: keep the longest excerpts (more substantive) up to max_per_source.
        Global: truncate to max_total after per-source filtering.
        """
        if len(evidence) <= max_total:
            # Check if per-source cap is needed
            by_url: dict[str, list[Evidence]] = defaultdict(list)
            for ev in evidence:
                by_url[ev.url].append(ev)
            if all(len(group) <= max_per_source for group in by_url.values()):
                return evidence  # already within both caps

        # Group by source URL
        by_url: dict[str, list[Evidence]] = defaultdict(list)
        for ev in evidence:
            by_url[ev.url].append(ev)

        # Per-source cap: keep longest excerpts
        capped: list[Evidence] = []
        for url in by_url:
            group = sorted(by_url[url], key=lambda e: len(e.excerpt), reverse=True)
            capped.extend(group[:max_per_source])

        if len(capped) > max_total:
            # Global cap: keep longest across all sources
            capped.sort(key=lambda e: len(e.excerpt), reverse=True)
            capped = capped[:max_total]

        dropped = len(evidence) - len(capped)
        if dropped > 0:
            logger.info(
                "Evidence cap: %d → %d (%d dropped, %d sources)",
                len(evidence),
                len(capped),
                dropped,
                len(by_url),
            )

        return capped

    # -- Quality heuristics (simple for Phase 1, refined later) --

    @staticmethod
    def _looks_peer_reviewed(result: CrawlResult) -> bool:
        """Basic heuristic: DOI in metadata or URL patterns."""
        url_lower = result.url.lower()
        indicators = ["doi.org", "pubmed", "ncbi.nlm.nih.gov", "arxiv.org", "scholar.google"]
        return any(ind in url_lower for ind in indicators)

    @staticmethod
    def _looks_primary(result: CrawlResult) -> bool:
        """Heuristic: .gov, .edu, or known research domains."""
        domain = urlparse(result.url).netloc.lower()
        return any(domain.endswith(suffix) for suffix in [".gov", ".edu", ".org"])

    @staticmethod
    def _assess_reputation(url: str, rules: ReputationRule) -> str:
        """Map a URL to a reputation tier based on policy rules."""
        domain = urlparse(url).netloc.lower()
        for trusted in rules.trusted_institutions:
            if trusted.lower() in domain:
                return "trusted"
        if any(domain.endswith(suffix) for suffix in [".gov", ".edu"]):
            return "institutional"
        return "unknown"

    @staticmethod
    def _looks_marketing(result: CrawlResult) -> bool:
        """Basic heuristic for marketing/sponsored content."""
        indicators = [
            "sponsored", "advertisement", "promoted", "affiliate",
            "buy now", "sign up free", "limited time offer",
        ]
        text_lower = (result.markdown[:2000] + result.title).lower()
        return any(ind in text_lower for ind in indicators)
