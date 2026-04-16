"""Tests for SQLiteEvidenceStore.get_existing_urls + get_by_urls (audit P3)."""

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


# ---------------------------------------------------------------------------
# get_by_urls
# ---------------------------------------------------------------------------


async def test_get_by_urls_empty_input(sqlite_store):
    assert await sqlite_store.get_by_urls([]) == []


async def test_get_by_urls_returns_matching_evidence(sqlite_store):
    urls = ["https://a.example.com", "https://b.example.com"]
    await _seed_urls(sqlite_store, urls)

    result = await sqlite_store.get_by_urls(urls)

    assert len(result) == 2
    assert {ev.url for ev in result} == set(urls)


async def test_get_by_urls_ignores_unknown_urls(sqlite_store):
    await _seed_urls(sqlite_store, ["https://seeded.example.com"])

    result = await sqlite_store.get_by_urls(
        ["https://seeded.example.com", "https://missing.example.com"]
    )

    assert len(result) == 1
    assert result[0].url == "https://seeded.example.com"


async def test_get_by_urls_returns_multiple_per_url(sqlite_store):
    """Same URL can back multiple evidence rows (chunks of one page)."""
    url = "https://article.example.com"
    # Two distinct excerpts at the same URL — simulates chunking.
    from tests.conftest import make_evidence

    ev_a = make_evidence(
        id="ev_chunk_a",
        url=url,
        excerpt="First chunk of the article with enough distinct content here.",
    )
    ev_b = make_evidence(
        id="ev_chunk_b",
        url=url,
        excerpt="Second chunk of the article with different distinct content.",
    )
    await sqlite_store.put_many([ev_a, ev_b])

    result = await sqlite_store.get_by_urls([url])

    assert {ev.id for ev in result} == {"ev_chunk_a", "ev_chunk_b"}
