"""Crawl adapter protocol.

Any source that can fetch web pages implements this protocol. The discoverer
calls into the adapter and receives raw crawl results, which it then
normalizes into Evidence objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CrawlResult:
    """Raw output from a single page crawl.

    This is the adapter's contract -- it returns these, and the discoverer
    converts them into Evidence objects. Kept as a plain dataclass (not
    Pydantic) to avoid coupling adapters to the model layer.
    """

    url: str
    status_code: int
    title: str = ""
    author: str = ""
    published_date: str = ""  # ISO 8601 if available, empty otherwise
    markdown: str = ""  # cleaned page content as markdown
    raw_html: str = ""  # original HTML if needed for re-extraction
    metadata: dict = field(default_factory=dict)  # adapter-specific extras


@dataclass(frozen=True)
class CrawlRequest:
    """What to crawl. Built by the discoverer from the CurationRequest + policy."""

    url: str
    timeout_seconds: int = 30
    extract_metadata: bool = True
    render_js: bool = False  # whether to run a headless browser


@runtime_checkable
class CrawlAdapter(Protocol):
    """Interface for fetching and extracting web page content."""

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        """Fetch a single URL and return its content."""
        ...

    async def crawl_many(self, requests: list[CrawlRequest]) -> list[CrawlResult]:
        """Fetch multiple URLs. Implementations should handle rate limiting."""
        ...

    async def search(self, query: str, limit: int = 10) -> list[str]:
        """Search for URLs relevant to a query. Returns a list of URLs to crawl.

        Not all adapters support search -- those that don't should raise
        NotImplementedError, and the discoverer will fall back to seed URLs
        from the curation request.
        """
        ...
