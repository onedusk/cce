"""Composite crawl adapter: one adapter per URL scheme (B14).

Lets web and non-web sources mix in one run. A consumer registers its own
adapter for a pseudo-URL scheme (``local://acme/report.pdf``,
``gdrive://<id>``) next to the web adapter and injects the composite through
``ComponentOverrides(crawl_adapter=...)``.

A pseudo-URL needs a host part (``scheme://host/...``): the source policy
matches its allow and deny lists against the host and drops a URL without
one, so ``file:///x.pdf`` never reaches an adapter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from itertools import zip_longest
from urllib.parse import urlparse

from cce.discovery.adapters.base import CrawlAdapter, CrawlRequest, CrawlResult

logger = logging.getLogger(__name__)


class CompositeCrawlAdapter:
    """Dispatch each request to the adapter registered for its URL scheme.

    ``by_scheme`` maps a lower-case scheme (``"local"``, ``"https"``) to an
    adapter; ``default`` takes every other scheme (typically the web
    adapter). A URL with no adapter comes back as a failed crawl
    (``status_code=0``), so discovery counts it and carries on.
    """

    def __init__(
        self,
        by_scheme: Mapping[str, CrawlAdapter],
        *,
        default: CrawlAdapter | None = None,
    ) -> None:
        self._by_scheme = {scheme.lower(): a for scheme, a in by_scheme.items()}
        self._default = default
        # Distinct adapters, default first, for search().
        self._adapters: list[CrawlAdapter] = []
        for adapter in (default, *self._by_scheme.values()):
            if adapter is not None and not any(adapter is a for a in self._adapters):
                self._adapters.append(adapter)

    def _adapter_for(self, url: str) -> CrawlAdapter | None:
        try:
            scheme = urlparse(url).scheme.lower()
        except ValueError:
            return None
        return self._by_scheme.get(scheme, self._default)

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        adapter = self._adapter_for(request.url)
        if adapter is None:
            logger.warning("No crawl adapter for the scheme of %.80r", request.url)
            return CrawlResult(url=request.url, status_code=0)
        return await adapter.crawl(request)

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        """Each adapter gets its own requests in one ``crawl_many`` call, all
        adapters concurrently; results come back in request order. An adapter
        that returns fewer results than requests leaves the rest as failed
        crawls; extra results (e.g. child pages) are appended at the end.
        """
        groups: dict[int, tuple[CrawlAdapter, list[int]]] = {}
        results: list[CrawlResult | None] = [None] * len(requests)
        for i, request in enumerate(requests):
            adapter = self._adapter_for(request.url)
            if adapter is None:
                logger.warning("No crawl adapter for the scheme of %.80r", request.url)
                continue
            groups.setdefault(id(adapter), (adapter, []))[1].append(i)

        batches = await asyncio.gather(
            *(
                adapter.crawl_many([requests[i] for i in indexes])
                for adapter, indexes in groups.values()
            )
        )
        extras: list[CrawlResult] = []
        for (_, indexes), batch in zip(groups.values(), batches, strict=True):
            for i, result in zip(indexes, batch, strict=False):
                results[i] = result
            extras.extend(batch[len(indexes) :])
        return [
            r if r is not None else CrawlResult(url=requests[i].url, status_code=0)
            for i, r in enumerate(results)
        ] + extras

    async def search(self, query: str, limit: int = 10) -> list[str]:
        """Ask every adapter that supports search, each for up to ``limit``
        URLs, and interleave the answers (first of each, second of each, ...)
        without duplicates. Interleaving keeps a later cap on sources from
        cutting out one adapter entirely. Raises ``NotImplementedError`` only
        when no adapter supports search. An adapter whose search fails is
        skipped while another finds URLs; when none does, its error is
        raised so the discoverer counts the failure (OPS-09).
        """
        found: list[list[str]] = []
        error: Exception | None = None
        for adapter in self._adapters:
            try:
                found.append(await adapter.search(query, limit=limit))
            except NotImplementedError:
                continue
            except Exception as e:
                error = e
        merged = [url for row in zip_longest(*found) for url in row if url is not None]
        if error is not None and not merged:
            raise error
        if not found:
            raise NotImplementedError("no adapter in the composite supports search")
        return list(dict.fromkeys(merged))
