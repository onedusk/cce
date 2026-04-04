"""Tests for FirecrawlAdapter with mocked SDK."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest
from cce.discovery.adapters.firecrawl import FirecrawlAdapter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> CrawlConfig:
    return CrawlConfig(
        adapter="firecrawl",
        api_key="test-fc-key",
        rate_limit_rps=10.0,
        timeout_seconds=5,
    )


def _mock_document(
    markdown: str = "# Page Title\n\nSome content here.",
    status_code: int = 200,
    metadata: dict | None = None,
    title: str = "",
) -> MagicMock:
    """Build a mock Firecrawl Document response object."""
    doc = MagicMock()
    doc.markdown = markdown
    doc.status_code = status_code
    doc.metadata = metadata if metadata is not None else {"title": title or "Test Page"}
    doc.html = "<html><body>content</body></html>"
    doc.title = title
    return doc


def _mock_search_result(url: str) -> MagicMock:
    """Build a mock search result item with a .url attribute."""
    item = MagicMock()
    item.url = url
    return item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_crawl_success(mock_fc_cls: MagicMock) -> None:
    """scrape returns mock document -> CrawlResult with markdown content."""
    mock_app = MagicMock()
    mock_app.scrape = MagicMock(
        return_value=_mock_document(
            markdown="# Article\n\nParagraph one.",
            status_code=200,
            metadata={"title": "Article"},
        )
    )
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    result = await adapter.crawl(CrawlRequest(url="https://example.com/page", timeout_seconds=5))

    assert result.url == "https://example.com/page"
    assert result.status_code == 200
    assert "Paragraph one" in result.markdown


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_crawl_failure(mock_fc_cls: MagicMock) -> None:
    """scrape raises Exception -> CrawlResult with status_code=0 and error metadata."""
    mock_app = MagicMock()
    mock_app.scrape = MagicMock(side_effect=Exception("Network timeout"))
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    result = await adapter.crawl(CrawlRequest(url="https://down.com/page", timeout_seconds=5))

    assert result.url == "https://down.com/page"
    assert result.status_code == 0
    assert "error" in result.metadata
    assert "Network timeout" in result.metadata["error"]


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_crawl_many(mock_fc_cls: MagicMock) -> None:
    """crawl_many with 3 URLs returns 3 CrawlResults."""
    mock_app = MagicMock()
    mock_app.scrape = MagicMock(
        return_value=_mock_document(markdown="Content", status_code=200)
    )
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    requests = [
        CrawlRequest(url=f"https://site{i}.com/page", timeout_seconds=5)
        for i in range(3)
    ]
    results = await adapter.crawl_many(requests)

    assert len(results) == 3
    for i, result in enumerate(results):
        assert result.url == f"https://site{i}.com/page"
        assert result.status_code == 200


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_search_success(mock_fc_cls: MagicMock) -> None:
    """search returns mock with .web list -> URL list."""
    mock_app = MagicMock()
    search_response = MagicMock()
    search_response.web = [
        _mock_search_result("https://result1.com"),
        _mock_search_result("https://result2.com"),
    ]
    search_response.news = None
    mock_app.search = MagicMock(return_value=search_response)
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    urls = await adapter.search("test query", limit=5)

    assert urls == ["https://result1.com", "https://result2.com"]


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_search_failure(mock_fc_cls: MagicMock) -> None:
    """search raises Exception -> empty list returned."""
    mock_app = MagicMock()
    mock_app.search = MagicMock(side_effect=Exception("Search API error"))
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    urls = await adapter.search("broken query")

    assert urls == []


@patch("cce.discovery.adapters.firecrawl.FirecrawlApp")
async def test_crawl_none_response(mock_fc_cls: MagicMock) -> None:
    """scrape returns None -> CrawlResult with status_code=0."""
    mock_app = MagicMock()
    mock_app.scrape = MagicMock(return_value=None)
    mock_fc_cls.return_value = mock_app

    adapter = FirecrawlAdapter(_config())
    result = await adapter.crawl(CrawlRequest(url="https://null.com/page", timeout_seconds=5))

    assert result.url == "https://null.com/page"
    assert result.status_code == 0
