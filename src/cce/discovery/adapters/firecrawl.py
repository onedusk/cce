"""Firecrawl adapter.

Uses the Firecrawl API (firecrawl-py SDK v4+) for crawling and search.
Phase 1 default adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from firecrawl import FirecrawlApp

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest, CrawlResult

logger = logging.getLogger(__name__)


class FirecrawlAdapter:
    """Firecrawl-backed crawl adapter (v4+ SDK)."""

    def __init__(self, config: CrawlConfig) -> None:
        self._config = config
        self._client = FirecrawlApp(api_key=config.api_key or "")
        self._semaphore = asyncio.Semaphore(max(1, int(config.rate_limit_rps)))

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
        """Fetch multiple URLs concurrently, respecting rate limit."""
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
