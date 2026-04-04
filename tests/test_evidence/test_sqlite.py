"""Tests for cce.evidence.sqlite — SQLiteEvidenceStore CRUD, dedup, search, serialization."""

from datetime import datetime, timezone

import pytest

from cce.models.evidence import SourceQuality
from tests.conftest import make_evidence

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_connect_creates_schema(sqlite_store):
    db = sqlite_store._db
    # Check evidence table exists
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None

    # Check _meta table exists
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None

    # Check schema version
    async with db.execute(
        "SELECT value FROM _meta WHERE key='schema_version'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "3"


# ---------------------------------------------------------------------------
# put / get
# ---------------------------------------------------------------------------


async def test_put_and_get(sqlite_store):
    ev = make_evidence(id="ev_test_001")
    await sqlite_store.put(ev)
    retrieved = await sqlite_store.get("ev_test_001")

    assert retrieved is not None
    assert retrieved.id == ev.id
    assert retrieved.url == ev.url
    assert retrieved.title == ev.title
    assert retrieved.author == ev.author
    assert retrieved.excerpt == ev.excerpt
    assert retrieved.excerpt_hash == ev.excerpt_hash


async def test_put_returns_true(sqlite_store):
    ev = make_evidence()
    result = await sqlite_store.put(ev)
    assert result is True


async def test_put_duplicate_hash_returns_false(sqlite_store):
    excerpt = "Shared excerpt content that is long enough to matter."
    ev1 = make_evidence(id="ev_first", excerpt=excerpt)
    ev2 = make_evidence(id="ev_second", excerpt=excerpt)
    assert ev1.excerpt_hash == ev2.excerpt_hash

    assert await sqlite_store.put(ev1) is True
    assert await sqlite_store.put(ev2) is False


# ---------------------------------------------------------------------------
# put_many
# ---------------------------------------------------------------------------


async def test_put_many(sqlite_store):
    evidence = [
        make_evidence(id=f"ev_{i}", excerpt=f"Unique excerpt number {i} with enough length.")
        for i in range(5)
    ]
    count = await sqlite_store.put_many(evidence)
    assert count == 5


async def test_put_many_dedup(sqlite_store):
    shared_excerpt = "This excerpt is shared and will cause dedup by hash."
    evidence = [
        make_evidence(id="ev_a", excerpt="Unique excerpt A that is long enough to pass."),
        make_evidence(id="ev_b", excerpt="Unique excerpt B that is long enough to pass."),
        make_evidence(id="ev_c", excerpt="Unique excerpt C that is long enough to pass."),
        make_evidence(id="ev_d", excerpt=shared_excerpt),
        make_evidence(id="ev_e", excerpt=shared_excerpt),
    ]
    count = await sqlite_store.put_many(evidence)
    assert count == 4  # 3 unique + 1 of the 2 shared


async def test_put_many_batch_insert(sqlite_store):
    """Batch insert 50 unique evidence objects, verify all stored and retrievable."""
    evidence = [
        make_evidence(id=f"ev_batch_{i}", excerpt=f"Unique batch excerpt number {i} for large insert test.")
        for i in range(50)
    ]
    count = await sqlite_store.put_many(evidence)
    assert count == 50

    for ev in evidence:
        retrieved = await sqlite_store.get(ev.id)
        assert retrieved is not None, f"Missing evidence {ev.id}"
        assert retrieved.id == ev.id


async def test_put_many_batch_all_duplicates(sqlite_store):
    """Insert 10 evidence objects, then re-insert the same 10 — returns 0."""
    evidence = [
        make_evidence(id=f"ev_dup_{i}", excerpt=f"Duplicate batch excerpt number {i} for dedup test.")
        for i in range(10)
    ]
    first = await sqlite_store.put_many(evidence)
    assert first == 10

    second = await sqlite_store.put_many(evidence)
    assert second == 0


async def test_put_many_batch_mixed(sqlite_store):
    """Insert 10 unique, then re-insert those 10 plus 5 new — returns 5."""
    original = [
        make_evidence(id=f"ev_orig_{i}", excerpt=f"Original batch excerpt number {i} for mixed test.")
        for i in range(10)
    ]
    first = await sqlite_store.put_many(original)
    assert first == 10

    new_ones = [
        make_evidence(id=f"ev_new_{i}", excerpt=f"Brand new batch excerpt number {i} for mixed test.")
        for i in range(5)
    ]
    combined = original + new_ones
    second = await sqlite_store.put_many(combined)
    assert second == 5


async def test_put_many_empty_list(sqlite_store):
    """put_many with an empty list returns 0 immediately."""
    count = await sqlite_store.put_many([])
    assert count == 0


# ---------------------------------------------------------------------------
# get / get_many
# ---------------------------------------------------------------------------


async def test_get_nonexistent(sqlite_store):
    result = await sqlite_store.get("no-such-id")
    assert result is None


async def test_get_many(sqlite_store):
    for i in range(3):
        await sqlite_store.put(
            make_evidence(id=f"ev_{i}", excerpt=f"Excerpt {i} long enough for test.")
        )
    results = await sqlite_store.get_many(["ev_0", "ev_1"])
    assert len(results) == 2


async def test_get_many_empty_list(sqlite_store):
    results = await sqlite_store.get_many([])
    assert results == []


async def test_get_many_partial_miss(sqlite_store):
    await sqlite_store.put(make_evidence(id="ev_exists_1", excerpt="Exists one long enough."))
    await sqlite_store.put(make_evidence(id="ev_exists_2", excerpt="Exists two long enough."))
    results = await sqlite_store.get_many(["ev_exists_1", "ev_exists_2", "ev_missing"])
    assert len(results) == 2


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_by_url(sqlite_store):
    await sqlite_store.put(
        make_evidence(id="ev_a", url="https://example.com/article", excerpt="Excerpt A for URL search test.")
    )
    await sqlite_store.put(
        make_evidence(id="ev_b", url="https://other.org/page", excerpt="Excerpt B for URL search test.")
    )
    results = await sqlite_store.search(url="https://example.com")
    assert len(results) == 1
    assert results[0].id == "ev_a"


async def test_search_by_topic(sqlite_store):
    await sqlite_store.put(
        make_evidence(
            id="ev_ml",
            title="Machine Learning Advances",
            excerpt="Recent advances in machine learning have been significant.",
        )
    )
    await sqlite_store.put(
        make_evidence(
            id="ev_other",
            title="Cooking Tips",
            excerpt="Here are some great cooking tips for beginners.",
        )
    )
    results = await sqlite_store.search(topic="machine learning")
    assert len(results) == 1
    assert results[0].id == "ev_ml"


async def test_search_limit(sqlite_store):
    for i in range(10):
        await sqlite_store.put(
            make_evidence(id=f"ev_{i}", excerpt=f"Search limit test excerpt number {i}.")
        )
    results = await sqlite_store.search(limit=3)
    assert len(results) == 3


async def test_search_no_filters(sqlite_store):
    for i in range(5):
        await sqlite_store.put(
            make_evidence(id=f"ev_{i}", excerpt=f"No filter test excerpt number {i}.")
        )
    results = await sqlite_store.search()
    assert len(results) == 5


# ---------------------------------------------------------------------------
# exists_by_hash
# ---------------------------------------------------------------------------


async def test_exists_by_hash_true(sqlite_store):
    ev = make_evidence()
    await sqlite_store.put(ev)
    assert await sqlite_store.exists_by_hash(ev.excerpt_hash) is True


async def test_exists_by_hash_false(sqlite_store):
    assert await sqlite_store.exists_by_hash("nonexistent_hash_value") is False


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


async def test_count(sqlite_store):
    for i in range(7):
        await sqlite_store.put(
            make_evidence(id=f"ev_{i}", excerpt=f"Count test excerpt number {i} here.")
        )
    assert await sqlite_store.count() == 7


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


async def test_serialization_roundtrip(sqlite_store):
    ev = make_evidence(
        id="ev_roundtrip",
        url="https://example.com/full",
        title="Full Title",
        author="Full Author",
        published_at=datetime(2024, 6, 15, 12, 30, tzinfo=timezone.utc),
        excerpt="A complete evidence excerpt for roundtrip serialization testing.",
        locator="chunk:3",
        source_quality=SourceQuality(
            is_peer_reviewed=True,
            is_primary_source=True,
            domain_reputation="trusted",
            conflict_of_interest=False,
        ),
    )
    await sqlite_store.put(ev)
    retrieved = await sqlite_store.get("ev_roundtrip")

    assert retrieved is not None
    assert retrieved.id == ev.id
    assert retrieved.url == ev.url
    assert retrieved.title == ev.title
    assert retrieved.author == ev.author
    assert retrieved.published_at == ev.published_at
    assert retrieved.retrieved_at == ev.retrieved_at
    assert retrieved.excerpt == ev.excerpt
    assert retrieved.excerpt_hash == ev.excerpt_hash
    assert retrieved.locator == ev.locator
    assert retrieved.source_quality is not None
    assert retrieved.source_quality.is_peer_reviewed is True
    assert retrieved.source_quality.is_primary_source is True
    assert retrieved.source_quality.domain_reputation == "trusted"
    assert retrieved.source_quality.conflict_of_interest is False


async def test_serialization_nullable_fields(sqlite_store):
    ev = make_evidence(
        id="ev_nullable",
        title=None,
        author=None,
        published_at=None,
        source_quality=None,
        locator=None,
    )
    await sqlite_store.put(ev)
    retrieved = await sqlite_store.get("ev_nullable")

    assert retrieved is not None
    assert retrieved.title is None
    assert retrieved.author is None
    assert retrieved.published_at is None
    assert retrieved.source_quality is None


# ---------------------------------------------------------------------------
# sqlite-vec integration
# ---------------------------------------------------------------------------


async def test_vec_table_created_when_available(sqlite_store):
    """The evidence_vec virtual table should exist if sqlite-vec loaded."""
    if not sqlite_store.vec_available:
        pytest.skip("sqlite-vec not available in this environment")
    db = sqlite_store._db
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_vec'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None


async def test_put_and_search_embedding(sqlite_store):
    """Store and retrieve an embedding via KNN search."""
    if not sqlite_store.vec_available:
        pytest.skip("sqlite-vec not available in this environment")
    ev = make_evidence(id="ev_vec_test")
    await sqlite_store.put(ev)

    embedding = [1.0] * 768
    stored = await sqlite_store.put_embedding("ev_vec_test", embedding)
    assert stored is True

    results = await sqlite_store.search_by_embedding(embedding, k=5)
    assert len(results) >= 1
    ids = [r[0] for r in results]
    assert "ev_vec_test" in ids


async def test_search_by_embedding_empty_store(sqlite_store):
    """Searching an empty vec store returns empty results."""
    if not sqlite_store.vec_available:
        pytest.skip("sqlite-vec not available in this environment")
    results = await sqlite_store.search_by_embedding([0.0] * 768, k=5)
    assert results == []


# -- Schema v3: taxonomy columns --


async def test_schema_v3_columns_exist(sqlite_store):
    """Fresh DB should have tags and dimension_signals columns."""
    db = sqlite_store._db
    async with db.execute("PRAGMA table_info(evidence)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    assert "tags" in columns
    assert "dimension_signals" in columns


async def test_evidence_roundtrip_with_tags(sqlite_store):
    """Evidence with tags and dimension_signals survives put/get."""
    ev = make_evidence(
        id="ev_tagged",
        tags=["emotional", "physical"],
        dimension_signals={"emotional": "primary", "physical": "secondary"},
    )
    await sqlite_store.put(ev)
    loaded = await sqlite_store.get("ev_tagged")
    assert loaded is not None
    assert loaded.tags == ["emotional", "physical"]
    assert loaded.dimension_signals == {"emotional": "primary", "physical": "secondary"}


async def test_evidence_roundtrip_without_tags(sqlite_store):
    """Evidence without tags returns empty defaults after roundtrip."""
    ev = make_evidence(id="ev_no_tags")
    await sqlite_store.put(ev)
    loaded = await sqlite_store.get("ev_no_tags")
    assert loaded is not None
    assert loaded.tags == []
    assert loaded.dimension_signals == {}
