"""Source discoverer.

Owns the full discover + extract + normalize step. Takes a CurationRequest
and SourcePolicy, uses a CrawlAdapter to fetch pages, and produces
Evidence objects ready for the evidence store.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult
from cce.discovery.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from cce.models.evidence import Evidence, SourceQuality
from cce.models.request import CurationConstraints, CurationRequest
from cce.policy.types import ReputationRule, SourcePolicy

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on degenerate input."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class Discoverer:
    """Discovers sources, applies policy filters, extracts evidence."""

    def __init__(
        self,
        adapter: CrawlAdapter,
        config: CrawlConfig,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._embedding = embedding_provider

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

        # Step 5: Extract, filter, and normalize (with in-run dedup by excerpt hash)
        evidence: list[Evidence] = []
        seen_hashes: set[str] = set()
        filtered_date = 0
        filtered_reputation = 0
        for result in crawl_results:
            if result.status_code == 0 or not result.markdown.strip():
                logger.debug("Skipping empty or failed crawl: %s", result.url)
                continue

            extracted = self._extract_evidence(result, effective_policy)
            for ev in extracted:
                if not self._passes_date_filter(ev, effective_policy, request.constraints):
                    filtered_date += 1
                    continue
                if not self._passes_reputation_filter(ev, effective_policy.reputation):
                    filtered_reputation += 1
                    continue
                if ev.excerpt_hash not in seen_hashes:
                    seen_hashes.add(ev.excerpt_hash)
                    evidence.append(ev)

        if filtered_date or filtered_reputation:
            logger.info(
                "Discovery filters: %d dropped by date, %d dropped by reputation",
                filtered_date,
                filtered_reputation,
            )

        # Step 5.5: Compute embedding relevance scores (if available)
        relevance_scores: dict[str, float] | None = None
        if self._embedding is not None and evidence:
            try:
                relevance_scores = await self._compute_relevance_scores(
                    evidence, request.topic, request.subtopics,
                )
                logger.info(
                    "Embedding ranking: scored %d evidence objects",
                    len(relevance_scores),
                )
            except EmbeddingUnavailableError as e:
                logger.warning(
                    "Embedding unavailable, falling back to length-based ranking: %s", e
                )
                relevance_scores = None

        # Step 6: Cap evidence volume
        before_cap = len(evidence)
        evidence = self._cap_evidence(
            evidence,
            max_per_source=self._config.max_excerpts_per_source,
            max_total=self._config.max_evidence_total,
            prefer_recent=effective_policy.recency.prefer_recent,
            relevance_scores=relevance_scores,
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

    # -- Post-extraction filters --

    @staticmethod
    def _passes_date_filter(
        ev: Evidence,
        policy: SourcePolicy,
        constraints: CurationConstraints | None,
    ) -> bool:
        """Check if evidence meets date constraints from request + policy.

        Fail-open: evidence with no published_at always passes.
        """
        if ev.published_at is None:
            return True

        # Policy-level: max_age_days relative to retrieval time
        if policy.recency.max_age_days is not None:
            try:
                age_days = (ev.retrieved_at - ev.published_at).days
            except TypeError:
                return True  # fail-open on naive/aware mismatch
            if age_days > policy.recency.max_age_days:
                return False

        # Request-level: absolute date bounds
        if constraints:
            if constraints.date_from:
                try:
                    lower = datetime.fromisoformat(
                        constraints.date_from.replace("Z", "+00:00")
                    )
                    if ev.published_at < lower:
                        return False
                except (ValueError, TypeError):
                    pass  # fail-open on bad/naive date

            if constraints.date_to:
                try:
                    upper = datetime.fromisoformat(
                        constraints.date_to.replace("Z", "+00:00")
                    )
                    if ev.published_at > upper:
                        return False
                except (ValueError, TypeError):
                    pass  # fail-open on bad/naive date

        return True

    @staticmethod
    def _passes_reputation_filter(
        ev: Evidence,
        reputation: ReputationRule,
    ) -> bool:
        """Check if evidence meets reputation hard filters.

        Fail-open: evidence with no source_quality always passes.
        """
        if ev.source_quality is None:
            return True

        if reputation.require_peer_reviewed and not ev.source_quality.is_peer_reviewed:
            return False

        if reputation.require_primary_source and not ev.source_quality.is_primary_source:
            return False

        if reputation.block_marketing and ev.source_quality.conflict_of_interest:
            return False

        return True

    # -- Embedding relevance --

    async def _compute_relevance_scores(
        self,
        evidence: list[Evidence],
        topic: str,
        subtopics: list[str],
    ) -> dict[str, float]:
        """Compute embedding-based relevance scores for evidence against the topic.

        Returns a mapping of evidence.id -> relevance score (0.0-1.0).
        Raises EmbeddingUnavailableError if embedding fails.
        """
        if not evidence or self._embedding is None:
            return {}

        query_text = topic
        if subtopics:
            query_text += " " + " ".join(subtopics)

        # Embed everything in one call: [query, excerpt_0, excerpt_1, ...]
        texts = [query_text] + [ev.excerpt for ev in evidence]
        result = await self._embedding.embed(texts)

        query_vec = result.vectors[0]
        scores: dict[str, float] = {}
        for ev, vec in zip(evidence, result.vectors[1:]):
            scores[ev.id] = _cosine_similarity(query_vec, vec)

        return scores

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
    def _cap_sort_key(
        ev: Evidence,
        prefer_recent: bool,
        relevance_scores: dict[str, float] | None = None,
    ) -> tuple[float, float, int]:
        """Build a sort key for evidence capping.

        Dimensions (highest priority first):
        1. Relevance score (embedding similarity to topic, 0.0 if unavailable)
        2. Recency (timestamp if prefer_recent, 0.0 otherwise)
        3. Length (longer = more substantive, tiebreaker)
        """
        relevance = relevance_scores.get(ev.id, 0.0) if relevance_scores else 0.0
        recency = ev.published_at.timestamp() if prefer_recent and ev.published_at else 0.0
        return (relevance, recency, len(ev.excerpt))

    @staticmethod
    def _cap_evidence(
        evidence: list[Evidence],
        max_per_source: int,
        max_total: int,
        prefer_recent: bool = False,
        relevance_scores: dict[str, float] | None = None,
    ) -> list[Evidence]:
        """Cap evidence volume with per-source and global limits.

        Per-source: keep the best excerpts up to max_per_source.
        Global: truncate to max_total after per-source filtering.
        When relevance_scores is provided, evidence is ranked by semantic similarity.
        When prefer_recent is True, recency breaks ties among equal relevance.
        """
        has_ranking = bool(relevance_scores) or prefer_recent

        if len(evidence) <= max_total:
            # Check if per-source cap is needed
            by_url: dict[str, list[Evidence]] = defaultdict(list)
            for ev in evidence:
                by_url[ev.url].append(ev)
            if all(len(group) <= max_per_source for group in by_url.values()):
                if has_ranking:
                    return sorted(
                        evidence,
                        key=lambda e: Discoverer._cap_sort_key(
                            e, prefer_recent, relevance_scores
                        ),
                        reverse=True,
                    )
                return evidence  # already within both caps

        # Group by source URL
        by_url: dict[str, list[Evidence]] = defaultdict(list)
        for ev in evidence:
            by_url[ev.url].append(ev)

        def _sort_key(e: Evidence) -> tuple[float, float, int]:
            return Discoverer._cap_sort_key(e, prefer_recent, relevance_scores)

        # Per-source cap: keep best excerpts
        capped: list[Evidence] = []
        for url in by_url:
            group = sorted(by_url[url], key=_sort_key, reverse=True)
            capped.extend(group[:max_per_source])

        if len(capped) > max_total:
            # Global cap: keep best across all sources
            capped.sort(key=_sort_key, reverse=True)
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
