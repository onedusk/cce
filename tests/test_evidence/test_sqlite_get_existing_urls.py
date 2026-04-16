"""Tests for SQLiteEvidenceStore.get_existing_urls — pre-crawl URL dedup (audit P3)."""

from __future__ import annotations

import pytest

from tests.conftest import make_evidence

pytestmark = pytest.mark.integration


async def _seed_urls(store, urls: list[str]) -> None:
    """Seed the store with distinct evidence rows, one per url."""
    evidence = [
        make_evidence(
            id=f"ev_seed_{i}",
            url=url,
            excerpt=(
                f"Seed excerpt {i} — unique content long enough to satisfy validators."
            ),
        )
        for i, url in enumerate(urls)
    ]
    await store.put_many(evidence)


async def test_empty_input_returns_empty_set(sqlite_store):
    assert await sqlite_store.get_existing_urls([]) == set()


async def test_all_candidates_missing(sqlite_store):
    await _seed_urls(sqlite_store, ["https://example.com/seeded"])
    result = await sqlite_store.get_existing_urls(
        ["https://other.com/a", "https://other.com/b"]
    )
    assert result == set()


async def test_all_candidates_present(sqlite_store):
    seeded = [
        "https://a.example.com/1",
        "https://b.example.com/2",
        "https://c.example.com/3",
    ]
    await _seed_urls(sqlite_store, seeded)
    result = await sqlite_store.get_existing_urls(seeded)
    assert result == set(seeded)


async def test_mixed_candidates(sqlite_store):
    seeded = ["https://seed.example.com/1", "https://seed.example.com/2"]
    fresh = ["https://fresh.example.com/a", "https://fresh.example.com/b"]
    await _seed_urls(sqlite_store, seeded)
    result = await sqlite_store.get_existing_urls(seeded + fresh)
    assert result == set(seeded)


async def test_duplicates_in_input(sqlite_store):
    await _seed_urls(sqlite_store, ["https://a.example.com"])
    result = await sqlite_store.get_existing_urls(
        ["https://a.example.com", "https://a.example.com", "https://b.example.com"]
    )
    assert result == {"https://a.example.com"}


async def test_chunking_over_lookup_limit(sqlite_store):
    """600 seeded URLs + 600 fresh URLs crosses the 500-param chunk boundary."""
    seeded = [f"https://seeded.example.com/{i}" for i in range(600)]
    fresh = [f"https://fresh.example.com/{i}" for i in range(600)]
    await _seed_urls(sqlite_store, seeded)

    # Interleave so the chunking boundaries mix seeded + fresh.
    candidates: list[str] = []
    for s, f in zip(seeded, fresh, strict=True):
        candidates.append(s)
        candidates.append(f)

    result = await sqlite_store.get_existing_urls(candidates)
    assert result == set(seeded)
    assert len(result) == 600
