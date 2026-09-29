"""Source discoverer.

Owns the full discover + extract + normalize step. Takes a CurationRequest
and SourcePolicy, uses a CrawlAdapter to fetch pages, and produces
Evidence objects ready for the evidence store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult
from cce.discovery.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from cce.evidence.store import EvidenceStore
from cce.models.evidence import DiscoveryResult, Evidence, SourceQuality
from cce.models.request import CurationConstraints, CurationRequest
from cce.policy.types import (
    DEFAULT_MARKETING_PHRASES,
    DEFAULT_PRIMARY_SOURCE_SUFFIXES,
    ReputationRule,
    SourcePolicy,
)

logger = logging.getLogger(__name__)

# DEFERRED (audit M2): subdivide this module into a discovery/discoverer/
# package (filtering.py, ranking.py, dispatcher.py) before Phase 4 adds
# more complexity here. Current single-file size is at the upper bound
# of comfortable scope (~700 LOC post-sprint).
# See docs/internal/improvement-opportunities-2026-06-09.md §0.2 (audit §M2).


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on degenerate input."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


_CRAWL_FAILURE_WARN_THRESHOLD = 0.3
SEARCH_RESULT_LIMIT = 20  # max URLs from search
MIN_FRAGMENT_SIZE = 50  # min chars for evidence excerpt
MAX_CHUNK_SIZE = 1500  # max chars per evidence chunk


# -- Discovery drop ledger (B10) ---------------------------------------------
#
# Two ledgers, because URL-level drops happen before any excerpt exists:
#   urls_gathered == urls_dropped_policy + urls_capped + urls_reused
#                    + crawl_failed + crawl_success
#   excerpts_gathered == dropped_fragment + dropped_date + dropped_reputation
#                        + dropped_marketing + deduplicated + capped + kept
# linked through crawl_success (pages whose chunks enter the excerpt ledger)
# and urls_reused / excerpts_reused (stored rows rehydrated for reused URLs).
_LEDGER_KEYS = (
    "urls_gathered",
    "urls_dropped_policy",
    "urls_capped",
    "urls_reused",
    "crawl_success",
    "crawl_failed",
    "excerpts_gathered",
    "excerpts_reused",
    "dropped_fragment",
    "dropped_date",
    "dropped_reputation",
    "dropped_marketing",
    "deduplicated",
    "capped",
    "kept",
)


# The ledger keys that are drops (logged when non-zero).
_DROP_KEYS = (
    "urls_dropped_policy",
    "urls_capped",
    "crawl_failed",
    "dropped_fragment",
    "dropped_date",
    "dropped_reputation",
    "dropped_marketing",
    "deduplicated",
    "capped",
)


def _log_drops(metrics: dict[str, int | float]) -> None:
    dropped = [f"{k}={metrics[k]}" for k in _DROP_KEYS if metrics.get(k)]
    if dropped:
        logger.info("Discovery drops: %s", ", ".join(dropped))


def _discovery_metrics(**counts: int) -> dict[str, int | float]:
    """Every ledger key (missing ones 0), plus crawl_failure_rate."""
    metrics: dict[str, int | float] = {key: counts.get(key, 0) for key in _LEDGER_KEYS}
    crawled = int(metrics["crawl_success"]) + int(metrics["crawl_failed"])
    metrics["crawl_failure_rate"] = (
        round(int(metrics["crawl_failed"]) / crawled, 2) if crawled else 0.0
    )
    return metrics


# -- Domain and phrase matching (B9) -----------------------------------------


def _url_host(url: str) -> str:
    """Lower-cased host without port, userinfo or trailing dot ('' if none)."""
    try:
        # WHATWG parsers (browsers) read "\" as "/" in http(s) URLs; urlparse
        # keeps it in the authority, so https://a.com\@b.com/ would be b.com.
        if urlparse(url).scheme in ("http", "https"):
            url = url.replace("\\", "/")
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".")


def _entry_labels(entry: str) -> list[str]:
    """Policy domain entry -> labels; tolerates '.gov', '*.example.com'."""
    normalized = entry.strip().lower().lstrip("*").strip(".")
    return normalized.split(".") if normalized else []


def _host_matches(host: str, entry: str) -> bool:
    """True when ``host`` is ``entry`` or a subdomain of it."""
    labels = _entry_labels(entry)
    if not labels:
        return False
    suffix = ".".join(labels)
    return host == suffix or host.endswith("." + suffix)


def _host_contains(host: str, entry: str) -> bool:
    """True when ``entry``'s labels appear as a contiguous run in ``host``."""
    labels = _entry_labels(entry)
    if not labels:
        return False
    host_labels = host.split(".")
    n = len(labels)
    return any(
        host_labels[i : i + n] == labels for i in range(len(host_labels) - n + 1)
    )


@lru_cache(maxsize=64)
def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive matcher for any phrase (None if empty).

    Lookarounds rather than \\b so phrases with non-word edges still work;
    words within a phrase may be separated by any whitespace (line wraps).
    """
    alternatives = [
        r"\s+".join(re.escape(word) for word in phrase.split())
        for phrase in phrases
        if phrase.strip()
    ]
    if not alternatives:
        return None
    return re.compile(
        r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE
    )


class Discoverer:
    """Discovers sources, applies policy filters, extracts evidence."""

    def __init__(
        self,
        adapter: CrawlAdapter,
        config: CrawlConfig,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_batch_size: int = 64,
        embedding_concurrency: int = 1,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._embedding = embedding_provider
        self._embedding_batch_size = embedding_batch_size
        self._embedding_concurrency = max(1, embedding_concurrency)
        self._evidence_store = evidence_store

    async def _split_fresh_and_reusable(
        self, candidates: list[str]
    ) -> tuple[list[str], list[Evidence]]:
        """Split candidates into (fresh_urls_to_crawl, reusable_stored_evidence).

        When an evidence store is wired in, URLs already indexed are moved off
        the crawl path (saving Firecrawl cost, audit P3) and their stored
        Evidence objects are rehydrated into the run so the rest of the
        pipeline has material to work with. No-op when no store is wired.
        """
        if not candidates or self._evidence_store is None:
            return candidates, []
        already = await self._evidence_store.get_existing_urls(candidates)
        if not already:
            return candidates, []
        fresh = [u for u in candidates if u not in already]
        reusable = await self._evidence_store.get_by_urls(list(already))
        logger.info(
            "URL dedup: %d/%d candidates already indexed "
            "(reusing %d stored evidence rows)",
            len(candidates) - len(fresh),
            len(candidates),
            len(reusable),
        )
        return fresh, reusable

    async def discover(
        self,
        request: CurationRequest,
        policy: SourcePolicy,
    ) -> DiscoveryResult:
        """Run the full discovery pipeline for a curation request.

        1. Build search queries from the request topic + subtopics
        2. Search for URLs (via adapter) or use constraint-provided seed URLs
        3. Filter URLs against the source policy
        4. Crawl the filtered URLs
        5. Extract and normalize into Evidence objects

        Returns a DiscoveryResult carrying the evidence plus crawl metrics
        (finding 1.2, ADR-005 — replaces the mutable per-instance metrics
        side-channel).
        """
        # Steps 1-2: Build search queries and collect candidate URLs
        candidate_urls = await self._search_candidates(request)

        # Step 3: Filter against policy
        effective_policy = self._resolve_overrides(request.topic, policy)
        filtered_urls = self._apply_policy_filters(candidate_urls, effective_policy)
        urls_dropped_policy = len(candidate_urls) - len(filtered_urls)

        # Step 3b: Split into fresh URLs (need crawling) and reusable stored evidence
        # from previously-indexed URLs (audit P3). Happens before the max-sources cap
        # so the crawl budget is spent only on URLs that are actually fresh.
        fresh_urls, reusable_evidence = await self._split_fresh_and_reusable(
            filtered_urls
        )
        # Candidates are unique and reused URLs are a subset of them.
        n_reused_urls = len(filtered_urls) - len(fresh_urls)
        fresh_overflow = max(0, len(fresh_urls) - policy.max_sources_per_run)
        reused_urls_before_cap = {ev.url for ev in reusable_evidence}

        # Cap total sources at policy.max_sources_per_run (review finding F-3).
        # Fresh URLs keep priority; reusable evidence fills the remaining
        # headroom. Cap is measured in UNIQUE URLs so a chunked page (multiple
        # Evidence rows per URL) contributes one "source" either way. Honors
        # the docstring contract "Cap on total sources discovered per curation
        # run"; previously the cap only bounded fresh URLs.
        fresh_urls = fresh_urls[: policy.max_sources_per_run]
        reusable_url_cap = max(0, policy.max_sources_per_run - len(fresh_urls))
        if reusable_url_cap == 0:
            reusable_evidence = []
        elif reusable_evidence:
            # Keep all evidence rows for the first `reusable_url_cap` unique URLs.
            seen_urls: set[str] = set()
            kept: list[Evidence] = []
            for ev in reusable_evidence:
                if ev.url in seen_urls:
                    kept.append(ev)  # additional chunks from an already-kept URL
                elif len(seen_urls) < reusable_url_cap:
                    seen_urls.add(ev.url)
                    kept.append(ev)
            reusable_evidence = kept
        reused_capped = len(reused_urls_before_cap) - len(
            {ev.url for ev in reusable_evidence}
        )
        url_ledger = {
            "urls_gathered": len(candidate_urls),
            "urls_dropped_policy": urls_dropped_policy,
            "urls_capped": fresh_overflow + reused_capped,
            # A URL the store reports but has no rows for counts as reused
            # with zero excerpts, so the URL identity stays exact.
            "urls_reused": n_reused_urls - reused_capped,
        }
        logger.info(
            "Discovery: %d fresh URLs to crawl, %d reusable evidence rows",
            len(fresh_urls),
            len(reusable_evidence),
        )

        if not fresh_urls and not reusable_evidence:
            logger.warning("Discovery: no URLs survived policy filter")
            metrics = _discovery_metrics(**url_ledger)
            _log_drops(metrics)
            return DiscoveryResult(evidence=[], metrics=metrics)

        # Steps 4-5: Crawl fresh URLs, extract + filter evidence, merge reusable
        evidence, metrics = await self._crawl_and_extract(
            fresh_urls, reusable_evidence, effective_policy, request.constraints
        )

        # Step 5.5: Compute embedding relevance scores (if available)
        relevance_scores: dict[str, float] | None = None
        if self._embedding is not None and evidence:
            try:
                relevance_scores = await self._compute_relevance_scores(
                    evidence,
                    request.topic,
                    request.subtopics,
                )
                logger.info(
                    "Embedding ranking: scored %d evidence objects",
                    len(relevance_scores),
                )
            except EmbeddingUnavailableError as e:
                # PDR-002: name the provider, where it was expected, and the
                # off switch, so the operator can either fix Ollama or disable
                # embedding ranking deliberately.
                base_url = getattr(
                    getattr(self._embedding, "_config", None), "base_url", None
                )
                logger.warning(
                    "Embedding unavailable (Ollama at %s), falling back to "
                    "length-based ranking: %s. If embeddings are intentionally "
                    "off, set CCE_EMBEDDING_ENABLED=false to disable embedding "
                    "ranking and silence this warning.",
                    base_url or "unknown base URL",
                    e,
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

        metrics = _discovery_metrics(
            **url_ledger,
            **{k: int(v) for k, v in metrics.items() if k in _LEDGER_KEYS},
            capped=before_cap - len(evidence),
            kept=len(evidence),
        )
        _log_drops(metrics)

        # Every requested URL is tallied as exactly one success or failure
        # (a result the adapter never returned is a failure).
        pages_crawled = int(metrics["crawl_success"]) + int(metrics["crawl_failed"])
        logger.info(
            "Discovery complete: %d evidence objects from %d pages (%d before cap)",
            len(evidence),
            pages_crawled,
            before_cap,
        )
        return DiscoveryResult(evidence=evidence, metrics=metrics)

    async def _search_candidates(self, request: CurationRequest) -> list[str]:
        """Build search queries and collect deduplicated candidate URLs."""
        # Step 1: Build search queries
        queries = self._build_queries(request)
        logger.info(
            "Discovery: %d search queries for topic '%s'", len(queries), request.topic
        )

        # Step 2: Search for candidate URLs
        candidate_urls: list[str] = []
        for query in queries:
            try:
                urls = await self._adapter.search(query, limit=SEARCH_RESULT_LIMIT)
                candidate_urls.extend(urls)
            except NotImplementedError:
                logger.info(
                    "Adapter does not support search, skipping query: %s", query
                )

        # Add any seed domains from constraints as fallback
        if request.constraints and request.constraints.domains_allow:
            for domain in request.constraints.domains_allow:
                candidate_urls.append(f"https://{domain}")

        # Deduplicate
        candidate_urls = list(dict.fromkeys(candidate_urls))
        logger.info(
            "Discovery: %d candidate URLs before policy filter", len(candidate_urls)
        )
        return candidate_urls

    def _apply_policy_filters(
        self, candidate_urls: list[str], policy: SourcePolicy
    ) -> list[str]:
        """Drop candidate URLs the (override-resolved) source policy rejects."""
        return [url for url in candidate_urls if self._passes_policy(url, policy)]

    async def _crawl_and_extract(
        self,
        fresh_urls: list[str],
        reusable_evidence: list[Evidence],
        effective_policy: SourcePolicy,
        constraints: CurationConstraints | None,
    ) -> tuple[list[Evidence], dict[str, int | float]]:
        """Crawl fresh URLs, extract + filter evidence, merge reusable rows.

        Returns ``(evidence, metrics)`` where metrics carries the
        crawl_success / crawl_failed / crawl_failure_rate keys previously
        stashed on the instance side-channel (finding 1.2).
        """
        # Step 4: Crawl fresh URLs (skip entirely if there are none to crawl)
        crawl_results: list[CrawlResult] = []
        if fresh_urls:
            crawl_requests = [
                CrawlRequest(
                    url=url,
                    timeout_seconds=self._config.timeout_seconds,
                )
                for url in fresh_urls
            ]
            crawl_results = await self._adapter.crawl_many(crawl_requests)

        # Step 5: Extract, filter, and normalize (with in-run dedup by excerpt
        # hash), counting every excerpt that doesn't survive by reason (B10).
        evidence: list[Evidence] = []
        seen_hashes: set[str] = set()
        counts: dict[str, int] = dict.fromkeys(
            (
                "excerpts_gathered",
                "dropped_fragment",
                "dropped_date",
                "dropped_reputation",
                "dropped_marketing",
                "deduplicated",
            ),
            0,
        )
        good_results = 0
        for result in crawl_results:
            if result.status_code == 0 or not result.markdown.strip():
                logger.debug("Skipping empty or failed crawl: %s", result.url)
                continue

            good_results += 1
            extracted, n_chunks = self._extract_evidence_counted(
                result, effective_policy
            )
            counts["excerpts_gathered"] += n_chunks
            counts["dropped_fragment"] += n_chunks - len(extracted)
            for ev in extracted:
                if not self._passes_date_filter(ev, effective_policy, constraints):
                    counts["dropped_date"] += 1
                    continue
                reason = self._reputation_drop_reason(ev, effective_policy.reputation)
                if reason is not None:
                    counts[f"dropped_{reason}"] += 1
                    continue
                if ev.excerpt_hash in seen_hashes:
                    counts["deduplicated"] += 1
                    continue
                seen_hashes.add(ev.excerpt_hash)
                evidence.append(ev)

        # One outcome per requested URL, so the URL ledger sums whatever the
        # adapter returns: missing results are failures, extra or duplicate
        # ones (an adapter adding child pages) don't count as more sources.
        # By count, not URL, since an adapter may report the redirected URL.
        crawl_success = min(good_results, len(fresh_urls))
        crawl_failed = len(fresh_urls) - crawl_success

        # Merge reusable evidence from previously-crawled URLs (audit P3).
        # Same excerpt-hash dedup applies so nothing is double-counted.
        counts["excerpts_gathered"] += len(reusable_evidence)
        for ev in reusable_evidence:
            if ev.excerpt_hash in seen_hashes:
                counts["deduplicated"] += 1
                continue
            seen_hashes.add(ev.excerpt_hash)
            evidence.append(ev)

        # Track crawl success/failure metrics
        total_crawls = crawl_success + crawl_failed
        failure_rate = crawl_failed / total_crawls if total_crawls else 0.0
        if failure_rate > _CRAWL_FAILURE_WARN_THRESHOLD:
            logger.warning(
                "High crawl failure rate: %d/%d (%.0f%%) URLs failed",
                crawl_failed,
                total_crawls,
                failure_rate * 100,
            )
        metrics: dict[str, int | float] = {
            "crawl_success": crawl_success,
            "crawl_failed": crawl_failed,
            "crawl_failure_rate": round(failure_rate, 2),
            "excerpts_reused": len(reusable_evidence),
            **counts,
        }

        return evidence, metrics

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
        return policy.resolve_for_topic(topic)

    @staticmethod
    def _passes_policy(url: str, policy: SourcePolicy) -> bool:
        """Check if a URL is allowed by the source policy.

        Matches on the URL's host at label boundaries, never substrings (B9):
        a deny entry matches when its labels appear as a contiguous run in the
        host (``x.com`` no longer blocks ``fox.com``; ``amazon.com`` still
        blocks ``amazon.com.au``), and an allow entry only when the host is it
        or ends with it (``nih.gov`` no longer admits ``nih.gov.evil.io``).
        """
        host = _url_host(url)
        if not host:
            return False

        # Deny list takes priority
        if any(_host_contains(host, denied) for denied in policy.domains_deny):
            return False

        # If allow list is non-empty, URL must match
        if policy.domains_allow and not any(
            _host_matches(host, allowed) for allowed in policy.domains_allow
        ):
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
        return Discoverer._reputation_drop_reason(ev, reputation) is None

    @staticmethod
    def _reputation_drop_reason(
        ev: Evidence, reputation: ReputationRule
    ) -> Literal["reputation", "marketing"] | None:
        """Why the reputation hard filters drop ``ev`` (None = kept), B10."""
        if ev.source_quality is None:
            return None

        if reputation.require_peer_reviewed and not ev.source_quality.is_peer_reviewed:
            return "reputation"

        if (
            reputation.require_primary_source
            and not ev.source_quality.is_primary_source
        ):
            return "reputation"

        if reputation.block_marketing and ev.source_quality.conflict_of_interest:
            return "marketing"

        return None

    # -- Embedding relevance --

    async def _embed_batches(self, texts: list[str]) -> list[list[float]]:
        """Dispatch embedding batches concurrently, preserving input order (audit P2).

        Concurrency is capped by `self._embedding_concurrency` — default 1
        keeps the behavior sequential on backends whose concurrency safety
        isn't verified. Output order matches input order so the caller can
        pair query vector + per-evidence vectors without re-mapping.
        """
        if not texts or self._embedding is None:
            return []

        # Bind to a local non-None reference so pyright can narrow inside the
        # nested closure (the `self._embedding is None` check at the top
        # doesn't narrow through the closure boundary).
        embedder = self._embedding
        size = self._embedding_batch_size
        batches = [texts[i : i + size] for i in range(0, len(texts), size)]
        semaphore = asyncio.Semaphore(self._embedding_concurrency)

        async def _one(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                result = await embedder.embed(batch)
                return result.vectors

        # Finding 2.3: batch timing visibility. The spec sketched
        # asyncio.get_event_loop().time(); time.monotonic() is the modern
        # equivalent (get_event_loop() outside a coroutine context is
        # deprecated) and measures the same monotonic clock.
        start = time.monotonic()
        per_batch = await asyncio.gather(*[_one(b) for b in batches])
        elapsed = time.monotonic() - start
        logger.info(
            "Embedded %d texts in %d batches (concurrency=%d): %.2fs",
            len(texts),
            len(batches),
            self._embedding_concurrency,
            elapsed,
        )
        return [v for group in per_batch for v in group]

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

        texts = [query_text] + [ev.excerpt for ev in evidence]
        all_vectors = await self._embed_batches(texts)

        if len(all_vectors) != len(texts):
            raise EmbeddingUnavailableError(
                f"Expected {len(texts)} vectors, got {len(all_vectors)}"
            )

        query_vec = all_vectors[0]
        scores: dict[str, float] = {}
        for ev, vec in zip(evidence, all_vectors[1:], strict=False):
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
        return self._extract_evidence_counted(result, policy)[0]

    def _extract_evidence_counted(
        self, result: CrawlResult, policy: SourcePolicy
    ) -> tuple[list[Evidence], int]:
        """``_extract_evidence`` plus the number of chunks considered, so the
        caller can count those dropped as too short (B10)."""
        chunks = self._chunk_content(result.markdown)
        now = datetime.now(UTC)

        quality = SourceQuality(
            is_peer_reviewed=self._looks_peer_reviewed(result),
            is_primary_source=self._looks_primary(
                result, policy.reputation.primary_source_suffixes
            ),
            domain_reputation=self._assess_reputation(result.url, policy.reputation),
            conflict_of_interest=self._looks_marketing(
                result, policy.reputation.marketing_phrases
            ),
        )

        evidence: list[Evidence] = []
        for i, chunk in enumerate(chunks):
            text = chunk.strip()
            if len(text) < MIN_FRAGMENT_SIZE:
                continue

            excerpt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            published_at = None
            pub_date = result.published_date
            if isinstance(pub_date, list):
                pub_date = pub_date[0] if pub_date else ""
            if pub_date:
                try:
                    published_at = datetime.fromisoformat(
                        pub_date.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass
                # A date-only or offset-less value is naive; read it as UTC so
                # the recency filter can compare it (it failed open before).
                if published_at is not None and published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)

            # Coerce metadata fields — adapters may return lists instead of strings
            title = (
                result.title
                if isinstance(result.title, str)
                else ", ".join(result.title)
                if result.title
                else None
            )
            author = (
                result.author
                if isinstance(result.author, str)
                else ", ".join(result.author)
                if result.author
                else None
            )

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

        return evidence, len(chunks)

    @staticmethod
    def _chunk_content(
        markdown: str, max_chunk_size: int = MAX_CHUNK_SIZE
    ) -> list[str]:
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
        recency = (
            ev.published_at.timestamp() if prefer_recent and ev.published_at else 0.0
        )
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
        indicators = [
            "doi.org",
            "pubmed",
            "ncbi.nlm.nih.gov",
            "arxiv.org",
            "scholar.google",
        ]
        return any(ind in url_lower for ind in indicators)

    @staticmethod
    def _looks_primary(
        result: CrawlResult,
        suffixes: Sequence[str] = DEFAULT_PRIMARY_SOURCE_SUFFIXES,
    ) -> bool:
        """Heuristic: the host ends with a primary-source suffix (B9: from the
        policy, label-boundary matched; default .gov / .edu)."""
        host = _url_host(result.url)
        return bool(host) and any(_host_matches(host, s) for s in suffixes)

    @staticmethod
    def _assess_reputation(url: str, rules: ReputationRule) -> str:
        """Map a URL to a reputation tier based on policy rules.

        Still substring-matched (not B9): policies rely on bare entries such
        as ``pubmed`` matching inside a host.
        """
        domain = urlparse(url).netloc.lower()
        for trusted in rules.trusted_institutions:
            if trusted.lower() in domain:
                return "trusted"
        if any(domain.endswith(suffix) for suffix in [".gov", ".edu"]):
            return "institutional"
        return "unknown"

    @staticmethod
    def _looks_marketing(
        result: CrawlResult,
        phrases: Sequence[str] = DEFAULT_MARKETING_PHRASES,
    ) -> bool:
        """Marketing/sponsored heuristic: any policy phrase, as whole words
        (B9: ``affiliate`` no longer matches ``affiliated``), in the title or
        the first 2,000 characters."""
        pattern = _phrase_pattern(tuple(phrases))
        if pattern is None:
            return False
        title = result.title
        if isinstance(title, list):  # adapters can return list metadata
            title = ", ".join(str(t) for t in title)
        text = result.markdown[:2000] + "\n" + (title or "")
        return pattern.search(text) is not None
