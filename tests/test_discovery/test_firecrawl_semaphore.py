"""Tests for the process-global FirecrawlAdapter semaphore registry (audit T4).

Covers T-03.03's implementation:
  - same (api_key, base_url) -> same semaphore object (identity)
  - different api_key -> distinct semaphore
  - combined RPS across multiple adapters respects the shared cap (the
    old per-instance semaphore silently doubled RPS for concurrent jobs)
  - test reset hook clears the registry
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.adapters.base import CrawlRequest
from cce.discovery.adapters.firecrawl import (
    _SEMAPHORES,
    FirecrawlAdapter,
    _reset_firecrawl_semaphores_for_tests,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_registry():
    """Clear the process-global semaphore registry around every test."""
    _reset_firecrawl_semaphores_for_tests()
    yield
    _reset_firecrawl_semaphores_for_tests()


# ---------------------------------------------------------------------------
# Identity: same key shares, different key doesn't
# ---------------------------------------------------------------------------


def test_same_key_shares_semaphore():
    a = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    b = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    assert a._semaphore is b._semaphore


def test_different_api_key_distinct_semaphore():
    a = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    c = FirecrawlAdapter(CrawlConfig(api_key="k2", rate_limit_rps=2.0))
    assert a._semaphore is not c._semaphore


# ---------------------------------------------------------------------------
# Reset hook
# ---------------------------------------------------------------------------


def test_reset_hook_clears_registry():
    FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    FirecrawlAdapter(CrawlConfig(api_key="k2", rate_limit_rps=2.0))
    assert len(_SEMAPHORES) == 2

    _reset_firecrawl_semaphores_for_tests()

    assert _SEMAPHORES == {}
    # Re-populate after reset
    FirecrawlAdapter(CrawlConfig(api_key="k3", rate_limit_rps=2.0))
    assert len(_SEMAPHORES) == 1


# ---------------------------------------------------------------------------
# Combined RPS behavior — two adapters share the cap
# ---------------------------------------------------------------------------


def _sleepy_scrape(sleep_s: float):
    """Build a sync scrape-like callable that sleeps then returns a minimal doc."""

    def _scrape(url, **_kwargs):
        time.sleep(sleep_s)
        return SimpleNamespace(
            markdown="content long enough to satisfy extraction",
            status_code=200,
            html="",
            metadata={"title": "T"},
        )

    return _scrape


async def test_combined_rps_respects_shared_cap():
    """Two adapters with same key, rate_limit_rps=2, fire 4 crawls -> ~2 waves.

    With the old per-instance semaphore each adapter would allow 2 in flight
    in parallel (4 total), so 4 concurrent crawls would finish in ~1 wave.
    With the shared registry the cap is 2 total, so 4 crawls finish in ~2
    waves and the wall-clock floor is 2 * sleep_s.
    """
    sleep_s = 0.1
    a = FirecrawlAdapter(CrawlConfig(api_key="shared", rate_limit_rps=2.0))
    b = FirecrawlAdapter(CrawlConfig(api_key="shared", rate_limit_rps=2.0))

    # Stub the underlying sync SDK client so each scrape just sleeps.
    a._client = SimpleNamespace(scrape=_sleepy_scrape(sleep_s))  # type: ignore[assignment]
    b._client = SimpleNamespace(scrape=_sleepy_scrape(sleep_s))  # type: ignore[assignment]

    reqs = [
        CrawlRequest(url=f"https://x.example/{i}", timeout_seconds=30) for i in range(4)
    ]

    t0 = time.monotonic()
    await asyncio.gather(
        a.crawl(reqs[0]), a.crawl(reqs[1]), b.crawl(reqs[2]), b.crawl(reqs[3])
    )
    wall = time.monotonic() - t0

    # Shared cap of 2 + 4 crawls + 0.1s each -> ~0.2s (two waves) with slop.
    # Per-instance semaphore (the old behavior) would finish in ~0.1s total.
    assert wall >= 1.8 * sleep_s, (
        f"Expected >={1.8 * sleep_s}s (two waves under shared cap); "
        f"got {wall:.3f}s — suggests semaphores are NOT shared across adapters."
    )


async def test_single_adapter_respects_own_cap():
    """Sanity: a single adapter with rate_limit_rps=2 still enforces its cap.

    Fire 4 crawls through one adapter; with sleep_s=0.1 and cap=2, expect
    ~2 waves -> ~0.2s.
    """
    sleep_s = 0.1
    a = FirecrawlAdapter(CrawlConfig(api_key="solo", rate_limit_rps=2.0))
    a._client = SimpleNamespace(scrape=_sleepy_scrape(sleep_s))  # type: ignore[assignment]

    reqs = [
        CrawlRequest(url=f"https://x.example/{i}", timeout_seconds=30) for i in range(4)
    ]

    t0 = time.monotonic()
    await asyncio.gather(*[a.crawl(r) for r in reqs])
    wall = time.monotonic() - t0

    assert wall >= 1.8 * sleep_s
