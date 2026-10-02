"""Firecrawl adapter.

Uses the Firecrawl API (firecrawl-py SDK v4+) for crawling and search.
Phase 1 default adapter.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from threading import Lock
from typing import Any

from firecrawl import FirecrawlApp

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest, CrawlResult

logger = logging.getLogger(__name__)


# --- Per-event-loop concurrency-cap registry (audit P4, ADR-002; CR-02) ---
# Per-instance semaphores silently doubled the combined in-flight requests
# whenever two jobs constructed their own FirecrawlAdapter. The registry here
# shares one asyncio.Semaphore across all adapters targeting the same
# (api_key, base_url) pair on the same event loop, so int(`rate_limit_rps`)
# caps the scrape requests in flight on that loop. It is a concurrency cap,
# not a per-second rate.
#
# The cap holds per event loop, not per process: an asyncio.Semaphore binds
# to the first loop that has to wait on it, so one semaphore shared across
# loops raised "is bound to a different event loop" on every later contended
# crawl (asyncio.run per job, or a loop per thread). A host running N loops
# at once can therefore have N times the cap in flight for one key.

_FIRECRAWL_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
# Weak keys drop a loop that is garbage collected. A semaphore that has had
# to wait holds a strong reference to its loop, which would keep that entry
# alive forever, so entries of closed loops are also dropped explicitly the
# next time a new loop registers.
_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Semaphore]
] = weakref.WeakKeyDictionary()
# First-registered `max_rps` per key, process-wide (plain ints, so no loop
# binding). Every loop's semaphore for the key is created with it, and a
# later adapter asking for a different capacity is warned (review F-4);
# asyncio.Semaphore's `_value` / `_initial_value` are private and unsafe to
# read from application code.
_SEMAPHORE_CAPACITIES: dict[tuple[str, str], int] = {}
_REGISTRY_LOCK = Lock()  # guards first-time insert; the semaphore itself is async.


def _register_capacity(*, api_key: str, base_url: str, max_rps: int) -> int:
    """Record the capacity for (api_key, base_url) once and return the one in force.

    If a subsequent call supplies a different `max_rps` for the same key, the
    first-registered capacity wins (there is no way to safely resize an
    already-awaited semaphore) and a warning is logged so the caller knows
    their requested cap was ignored.
    """
    key = (api_key, base_url)
    cap = max(1, max_rps)
    with _REGISTRY_LOCK:
        registered = _SEMAPHORE_CAPACITIES.setdefault(key, cap)
        if registered != cap:
            logger.warning(
                "Firecrawl semaphore for %s already registered with "
                "rate_limit_rps=%d; ignoring subsequent request for "
                "rate_limit_rps=%d.",
                base_url,
                registered,
                cap,
            )
    return registered


def _get_shared_semaphore(
    *, api_key: str, base_url: str, capacity: int
) -> asyncio.Semaphore:
    """Return the running loop's semaphore for (api_key, base_url), creating it once.

    Must be called from a coroutine: the semaphore is looked up under the
    running event loop, so each loop gets its own.
    """
    loop = asyncio.get_running_loop()
    key = (api_key, base_url)
    with _REGISTRY_LOCK:
        per_loop = _SEMAPHORES.get(loop)
        if per_loop is None:
            for closed in [lp for lp in _SEMAPHORES if lp.is_closed()]:
                del _SEMAPHORES[closed]
            per_loop = _SEMAPHORES[loop] = {}
        sem = per_loop.get(key)
        if sem is None:
            sem = per_loop[key] = asyncio.Semaphore(capacity)
    return sem


def _reset_firecrawl_semaphores_for_tests() -> None:
    """Clear the module-global semaphore registry. Tests only — never call in prod."""
    with _REGISTRY_LOCK:
        _SEMAPHORES.clear()
        _SEMAPHORE_CAPACITIES.clear()


class FirecrawlAdapter:
    """Firecrawl-backed crawl adapter (v4+ SDK)."""

    def __init__(self, config: CrawlConfig) -> None:
        self._config = config
        self._client = FirecrawlApp(api_key=config.api_key or "")
        self._base_url = (
            getattr(config, "base_url", None) or _FIRECRAWL_DEFAULT_BASE_URL
        )
        self._capacity = _register_capacity(
            api_key=config.api_key or "",
            base_url=self._base_url,
            max_rps=int(config.rate_limit_rps),
        )

    @property
    def _semaphore(self) -> asyncio.Semaphore:
        """The running loop's shared semaphore, resolved on each use (CR-02)."""
        return _get_shared_semaphore(
            api_key=self._config.api_key or "",
            base_url=self._base_url,
            capacity=self._capacity,
        )

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        """Fetch a single URL via Firecrawl's scrape endpoint."""
        async with self._semaphore:
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.scrape(
                        request.url,
                        formats=["markdown"],
                        timeout=request.timeout_seconds * 1000,
                    ),
                )
                return self._parse_response(request.url, response)
            except Exception as e:
                logger.warning("Firecrawl scrape failed for %s: %s", request.url, e)
                return CrawlResult(
                    url=request.url,
                    status_code=0,
                    metadata={"error": str(e)},
                )

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        """Fetch multiple URLs concurrently, respecting the concurrency cap."""
        tasks = [self.crawl(req) for req in requests]
        return await asyncio.gather(*tasks)

    async def search(self, query: str, limit: int = 10) -> list[str]:
        """Use Firecrawl's search endpoint to find relevant URLs."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client.search(query, limit=limit),
            )
            # v4 returns SearchData with .web, .news, .images lists
            urls: list[str] = []
            for source_list in (
                getattr(response, "web", None),
                getattr(response, "news", None),
            ):
                if source_list:
                    for r in source_list:
                        url = getattr(r, "url", None)
                        if url:
                            urls.append(url)
            return urls
        except Exception as e:
            logger.warning("Firecrawl search failed for query '%s': %s", query, e)
            return []

    @staticmethod
    def _parse_response(url: str, response: Any) -> CrawlResult:
        """Convert Firecrawl v4 Document response into a CrawlResult."""
        if response is None:
            return CrawlResult(url=url, status_code=0)

        # v4 returns a Document object with attributes
        # Try attribute access first, then dict fallback
        def _get(obj: Any, attr: str, default: Any = "") -> Any:
            if hasattr(obj, attr):
                val = getattr(obj, attr)
                return val if val is not None else default
            if isinstance(obj, dict):
                val = obj.get(attr)
                return val if val is not None else default
            return default

        metadata = _get(response, "metadata", {})
        if metadata is None:
            metadata = {}

        # metadata might also be an object with attributes
        def _meta(attr: str, default: str = "") -> str:
            if isinstance(metadata, dict):
                return metadata.get(attr, default) or default
            if hasattr(metadata, attr):
                val = getattr(metadata, attr)
                return val if val is not None else default
            return default

        return CrawlResult(
            url=url,
            status_code=_get(response, "status_code", 200) or 200,
            title=_meta("title") or _meta("og:title") or _get(response, "title", ""),
            author=_meta("author") or _meta("og:author", ""),
            published_date=(
                _meta("published_date")
                or _meta("article:published_time")
                or _meta("publishedTime", "")
            ),
            markdown=_get(response, "markdown", ""),
            raw_html=_get(response, "html", ""),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
