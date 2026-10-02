"""Tests for the per-event-loop FirecrawlAdapter semaphore registry (audit T4).

Covers T-03.03's implementation:
  - same (api_key, base_url) -> same semaphore object (identity)
  - different api_key -> distinct semaphore
  - combined RPS across multiple adapters respects the shared cap (the
    old per-instance semaphore silently doubled RPS for concurrent jobs)
  - test reset hook clears the registry

And CR-02 (audit 2026-10-02): the registry is keyed by the running event
loop as well, so a host with one loop per job (or per thread) keeps crawling.
"""

from __future__ import annotations

import asyncio
import threading
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


async def test_same_key_shares_semaphore():
    a = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    b = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    assert a._semaphore is b._semaphore


async def test_different_api_key_distinct_semaphore():
    a = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    c = FirecrawlAdapter(CrawlConfig(api_key="k2", rate_limit_rps=2.0))
    assert a._semaphore is not c._semaphore


# ---------------------------------------------------------------------------
# Reset hook
# ---------------------------------------------------------------------------


async def test_reset_hook_clears_registry():
    # Semaphores are created on first use under the running loop (CR-02).
    loop = asyncio.get_running_loop()
    a = FirecrawlAdapter(CrawlConfig(api_key="k1", rate_limit_rps=2.0))
    b = FirecrawlAdapter(CrawlConfig(api_key="k2", rate_limit_rps=2.0))
    assert a._semaphore is not b._semaphore
    assert len(_SEMAPHORES[loop]) == 2

    _reset_firecrawl_semaphores_for_tests()

    assert len(_SEMAPHORES) == 0
    # Re-populate after reset
    c = FirecrawlAdapter(CrawlConfig(api_key="k3", rate_limit_rps=2.0))
    assert c._semaphore is c._semaphore
    assert len(_SEMAPHORES[loop]) == 1


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


async def test_differing_max_rps_for_same_key_logs_warning(caplog):
    """Second adapter with a different rate_limit_rps gets a warning, not a silent merge (F-4)."""
    import logging

    first = FirecrawlAdapter(CrawlConfig(api_key="shared-key", rate_limit_rps=2.0))
    with caplog.at_level(logging.WARNING, logger="cce.discovery.adapters.firecrawl"):
        second = FirecrawlAdapter(CrawlConfig(api_key="shared-key", rate_limit_rps=5.0))

    # Both adapters still share the first-registered semaphore.
    assert first._semaphore is second._semaphore

    warnings = [r for r in caplog.records if "rate_limit_rps" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "2" in msg
    assert "5" in msg


def test_matching_max_rps_no_warning(caplog):
    """Same rate_limit_rps on second registration -> no warning."""
    import logging

    FirecrawlAdapter(CrawlConfig(api_key="k", rate_limit_rps=3.0))
    with caplog.at_level(logging.WARNING, logger="cce.discovery.adapters.firecrawl"):
        FirecrawlAdapter(CrawlConfig(api_key="k", rate_limit_rps=3.0))

    warnings = [r for r in caplog.records if "rate_limit_rps" in r.getMessage()]
    assert warnings == []


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


# ---------------------------------------------------------------------------
# CR-02: more than one event loop in the process
# ---------------------------------------------------------------------------

_CAP = 2
_URLS = 6  # more URLs than the cap, so crawl_many has to wait on the semaphore


def _stubbed_adapter() -> FirecrawlAdapter:
    """Adapter with cap 2 whose SDK client is a local stub (no network)."""
    adapter = FirecrawlAdapter(CrawlConfig(api_key="multi-loop", rate_limit_rps=_CAP))
    adapter._client = SimpleNamespace(scrape=_sleepy_scrape(0.01))  # type: ignore[assignment]
    return adapter


def _requests() -> list[CrawlRequest]:
    return [
        CrawlRequest(url=f"https://x.example/{i}", timeout_seconds=30)
        for i in range(_URLS)
    ]


def test_crawl_many_over_cap_under_two_event_loops():
    """One asyncio.run per job, a fresh adapter each: every job crawls (CR-02).

    The process-global semaphore was bound to the first loop that contended
    on it, so the second job raised "RuntimeError: ... is bound to a
    different event loop" out of crawl_many.
    """
    for _job in range(2):
        results = asyncio.run(_stubbed_adapter().crawl_many(_requests()))
        assert [r.status_code for r in results] == [200] * _URLS


def test_one_adapter_reused_across_event_loops():
    """One adapter built once (a shared ComponentSet), a new loop per job."""
    adapter = _stubbed_adapter()
    for _job in range(2):
        results = asyncio.run(adapter.crawl_many(_requests()))
        assert [r.status_code for r in results] == [200] * _URLS


def test_closed_loop_entries_are_dropped():
    """A loop that has closed does not stay in the registry.

    A semaphore that has had to wait references its loop, so the weak key
    alone never dies: the entry goes when the next new loop registers.
    """
    adapter = _stubbed_adapter()
    for _job in range(3):
        asyncio.run(adapter.crawl_many(_requests()))

    # Loops 1 and 2 went when loops 2 and 3 registered: at most loop 3 is left.
    assert len(_SEMAPHORES) <= 1
    assert all(loop.is_closed() for loop in _SEMAPHORES)

    async def _lookup() -> None:
        assert adapter._semaphore is adapter._semaphore
        assert list(_SEMAPHORES) == [asyncio.get_running_loop()]

    asyncio.run(_lookup())


def test_crawl_many_over_cap_with_a_loop_per_thread():
    """Two threads, each with its own loop and the same key, both crawl."""
    adapter = _stubbed_adapter()
    outcomes: list[list[int]] = []

    def _job() -> None:
        results = asyncio.run(adapter.crawl_many(_requests()))
        outcomes.append([r.status_code for r in results])

    threads = [threading.Thread(target=_job, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert outcomes == [[200] * _URLS] * 2
