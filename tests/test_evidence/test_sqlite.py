"""Tests for cce.evidence.sqlite — SQLiteEvidenceStore CRUD, dedup, search, serialization."""

from datetime import UTC, datetime

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
        assert row[0] == "4"


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
        make_evidence(
            id=f"ev_{i}", excerpt=f"Unique excerpt number {i} with enough length."
        )
        for i in range(5)
    ]
    count = await sqlite_store.put_many(evidence)
    assert count == 5


async def test_put_many_dedup(sqlite_store):
    shared_excerpt = "This excerpt is shared and will cause dedup by hash."
    evidence = [
        make_evidence(
            id="ev_a", excerpt="Unique excerpt A that is long enough to pass."
        ),
        make_evidence(
            id="ev_b", excerpt="Unique excerpt B that is long enough to pass."
        ),
        make_evidence(
            id="ev_c", excerpt="Unique excerpt C that is long enough to pass."
        ),
        make_evidence(id="ev_d", excerpt=shared_excerpt),
        make_evidence(id="ev_e", excerpt=shared_excerpt),
    ]
    count = await sqlite_store.put_many(evidence)
    assert count == 4  # 3 unique + 1 of the 2 shared


async def test_put_many_batch_insert(sqlite_store):
    """Batch insert 50 unique evidence objects, verify all stored and retrievable."""
    evidence = [
        make_evidence(
            id=f"ev_batch_{i}",
            excerpt=f"Unique batch excerpt number {i} for large insert test.",
        )
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
        make_evidence(
            id=f"ev_dup_{i}",
            excerpt=f"Duplicate batch excerpt number {i} for dedup test.",
        )
        for i in range(10)
    ]
    first = await sqlite_store.put_many(evidence)
    assert first == 10

    second = await sqlite_store.put_many(evidence)
    assert second == 0


async def test_put_many_batch_mixed(sqlite_store):
    """Insert 10 unique, then re-insert those 10 plus 5 new — returns 5."""
    original = [
        make_evidence(
            id=f"ev_orig_{i}",
            excerpt=f"Original batch excerpt number {i} for mixed test.",
        )
        for i in range(10)
    ]
    first = await sqlite_store.put_many(original)
    assert first == 10

    new_ones = [
        make_evidence(
            id=f"ev_new_{i}",
            excerpt=f"Brand new batch excerpt number {i} for mixed test.",
        )
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
    await sqlite_store.put(
        make_evidence(id="ev_exists_1", excerpt="Exists one long enough.")
    )
    await sqlite_store.put(
        make_evidence(id="ev_exists_2", excerpt="Exists two long enough.")
    )
    results = await sqlite_store.get_many(["ev_exists_1", "ev_exists_2", "ev_missing"])
    assert len(results) == 2


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_by_url(sqlite_store):
    await sqlite_store.put(
        make_evidence(
            id="ev_a",
            url="https://example.com/article",
            excerpt="Excerpt A for URL search test.",
        )
    )
    await sqlite_store.put(
        make_evidence(
            id="ev_b",
            url="https://other.org/page",
            excerpt="Excerpt B for URL search test.",
        )
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
            make_evidence(
                id=f"ev_{i}", excerpt=f"Search limit test excerpt number {i}."
            )
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
        published_at=datetime(2024, 6, 15, 12, 30, tzinfo=UTC),
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


# ---------------------------------------------------------------------------
# v4: UNIQUE(url, excerpt_hash) and stored-ID resolution (B6)
# ---------------------------------------------------------------------------

_SYNDICATED = "A syndicated excerpt that two outlets published word for word."


async def test_same_excerpt_at_two_urls_is_stored_twice(sqlite_store):
    """v1-v3 kept one row per excerpt, so the second URL's copy was never
    stored and was cited under an ID that did not exist."""
    a = make_evidence(
        id="ev_aaaaaaaaaaaa", url="https://a.example/x", excerpt=_SYNDICATED
    )
    b = make_evidence(
        id="ev_bbbbbbbbbbbb", url="https://b.example/y", excerpt=_SYNDICATED
    )

    assert await sqlite_store.put(a) is True
    assert await sqlite_store.put(b) is True

    assert await sqlite_store.count() == 2
    assert (await sqlite_store.get("ev_bbbbbbbbbbbb")).url == "https://b.example/y"


async def test_same_url_and_excerpt_is_still_a_duplicate(sqlite_store):
    first = make_evidence(id="ev_111111111111", excerpt=_SYNDICATED)
    again = make_evidence(id="ev_222222222222", excerpt=_SYNDICATED)

    assert await sqlite_store.put(first) is True
    assert await sqlite_store.put(again) is False
    assert await sqlite_store.put_many([again]) == 0


async def test_get_stored_ids_maps_only_same_url_collisions(sqlite_store):
    await sqlite_store.put(
        make_evidence(
            id="ev_5eed5eed5eed", url="https://a.example/x", excerpt=_SYNDICATED
        )
    )
    same_url = make_evidence(
        id="ev_newnewnewnew", url="https://a.example/x", excerpt=_SYNDICATED
    )
    other_url = make_evidence(
        id="ev_othrothrothr", url="https://b.example/y", excerpt=_SYNDICATED
    )
    unseen = make_evidence(
        id="ev_unseenunseen", excerpt="An excerpt no store has seen yet at all."
    )
    already_stored = make_evidence(
        id="ev_5eed5eed5eed", url="https://a.example/x", excerpt=_SYNDICATED
    )

    remap = await sqlite_store.get_stored_ids(
        [same_url, other_url, unseen, already_stored]
    )

    assert remap == {"ev_newnewnewnew": "ev_5eed5eed5eed"}


async def test_get_stored_ids_empty(sqlite_store):
    assert await sqlite_store.get_stored_ids([]) == {}


# --- v3 -> v4 migration -----------------------------------------------------

_V3_DDL = """
CREATE TABLE evidence (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, author TEXT,
    published_at TEXT, retrieved_at TEXT NOT NULL, excerpt TEXT NOT NULL,
    excerpt_hash TEXT NOT NULL, locator TEXT, source_quality TEXT,
    tags TEXT, dimension_signals TEXT,
    UNIQUE(excerpt_hash)
);
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO _meta VALUES ('schema_version', '3');
"""


async def _seed_legacy_db(path, ddl: str, rows: list[tuple]) -> None:
    import aiosqlite

    async with aiosqlite.connect(str(path)) as db:
        await db.executescript(ddl)
        placeholders = ",".join("?" * len(rows[0]))
        await db.executemany(f"INSERT INTO evidence VALUES ({placeholders})", rows)
        await db.commit()


def _legacy_row(ev_id: str, url: str, excerpt: str, *, v3: bool = True) -> tuple:
    import hashlib

    base = (
        ev_id,
        url,
        "Title",
        "Author",
        "2024-01-15T00:00:00+00:00",
        "2024-03-01T00:00:00+00:00",
        excerpt,
        hashlib.sha256(excerpt.encode()).hexdigest(),
        "chunk:0",
        None,
    )
    return base + ('["t1"]', '{"d": "strong"}') if v3 else base


async def _unique_index_columns(store) -> list[tuple[str, ...]]:
    found = []
    async with store._db.execute("PRAGMA index_list(evidence)") as cursor:
        indexes = await cursor.fetchall()
    for index in indexes:
        # origin "u" = a UNIQUE constraint (not the primary key's autoindex).
        if index[2] and index[3] == "u":
            async with store._db.execute(f"PRAGMA index_info('{index[1]}')") as c:
                found.append(tuple(row[2] for row in await c.fetchall()))
    return found


async def _index_names(store) -> set[str]:
    async with store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='evidence'"
    ) as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def test_v3_database_migrates_to_v4_losslessly(tmp_path):
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore

    db_path = tmp_path / "legacy.db"
    await _seed_legacy_db(
        db_path,
        _V3_DDL,
        [
            _legacy_row("ev_old000000001", "https://a.example/x", _SYNDICATED),
            _legacy_row("ev_old000000002", "https://a.example/z", "Another excerpt."),
        ],
    )

    store = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=db_path))
    await store.connect()
    try:
        assert await _unique_index_columns(store) == [("url", "excerpt_hash")]
        assert {
            "idx_evidence_url",
            "idx_evidence_hash",
            "idx_evidence_retrieved",
        } <= await _index_names(store)
        async with store._db.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == "4"

        old = await store.get("ev_old000000001")
        assert old is not None
        assert old.url == "https://a.example/x"
        assert old.title == "Title"
        assert old.locator == "chunk:0"
        assert old.tags == ["t1"]
        assert old.dimension_signals == {"d": "strong"}
        assert await store.count() == 2

        # The syndicated copy at a new URL is now accepted.
        syndicated = make_evidence(
            id="ev_new000000001", url="https://b.example/y", excerpt=_SYNDICATED
        )
        assert await store.put(syndicated) is True
    finally:
        await store.close()


async def test_migration_is_idempotent_and_survives_downgrade(tmp_path):
    """Reconnecting is a no-op; after an older binary rewrites
    schema_version=3 on the v4 table, reconnecting keeps the v4 shape."""
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore

    db_path = tmp_path / "legacy.db"
    await _seed_legacy_db(
        db_path, _V3_DDL, [_legacy_row("ev_old000000001", "https://a.example/x", "E.")]
    )
    config = EvidenceStoreConfig(sqlite_path=db_path)

    for _ in range(2):
        store = SQLiteEvidenceStore(config)
        await store.connect()
        await store.close()

    store = SQLiteEvidenceStore(config)
    await store.connect()
    await store._db.execute("UPDATE _meta SET value='3' WHERE key='schema_version'")
    await store._db.commit()
    await store.close()

    store = SQLiteEvidenceStore(config)
    await store.connect()
    try:
        assert await _unique_index_columns(store) == [("url", "excerpt_hash")]
        assert await store.count() == 1
    finally:
        await store.close()


async def test_pre_v3_table_without_meta_migrates_through_v3_and_v4(tmp_path):
    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore

    ddl = """
CREATE TABLE evidence (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, author TEXT,
    published_at TEXT, retrieved_at TEXT NOT NULL, excerpt TEXT NOT NULL,
    excerpt_hash TEXT NOT NULL, locator TEXT, source_quality TEXT,
    UNIQUE(excerpt_hash)
);
"""
    db_path = tmp_path / "v1.db"
    await _seed_legacy_db(
        db_path,
        ddl,
        [_legacy_row("ev_old000000001", "https://a.example/x", "E.", v3=False)],
    )

    store = SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=db_path))
    await store.connect()
    try:
        assert await _unique_index_columns(store) == [("url", "excerpt_hash")]
        old = await store.get("ev_old000000001")
        assert old is not None and old.tags == [] and old.dimension_signals == {}
    finally:
        await store.close()


async def test_concurrent_first_opens_of_a_v3_database_all_succeed(tmp_path, caplog):
    """Review of B6: the rebuild used a deferred BEGIN, so a second opener
    racing the first failed with 'database is locked' instead of waiting."""
    import asyncio
    import logging

    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore

    db_path = tmp_path / "legacy.db"
    rows = [
        _legacy_row(f"ev_old{i:09d}", f"https://a.example/{i}", f"Excerpt {i}.")
        for i in range(1000)  # a rebuild long enough to overlap (9/10 before)
    ]
    # WAL, as every store-created file is: a rollback-journal file would race
    # on the journal-mode switch in connect() instead.
    await _seed_legacy_db(db_path, "PRAGMA journal_mode=WAL;\n" + _V3_DDL, rows)
    stores = [
        SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=db_path)) for _ in range(3)
    ]

    with caplog.at_level(logging.INFO, logger="cce.evidence.sqlite"):
        results = await asyncio.gather(
            *(s.connect() for s in stores), return_exceptions=True
        )
    try:
        assert [r for r in results if isinstance(r, BaseException)] == []
        rebuilds = [
            r for r in caplog.records if "Migrating evidence table" in r.message
        ]
        assert len(rebuilds) == 1
        for store in stores:
            assert not store._db.in_transaction
            assert await store.count() == 1000
        assert await _unique_index_columns(stores[0]) == [("url", "excerpt_hash")]
    finally:
        for store in stores:
            await store.close()


async def test_opener_that_saw_the_old_shape_does_not_rebuild_again(tmp_path, caplog):
    """Deterministic form of the race: another opener migrates between this
    opener's shape check and its transaction. The check is repeated inside
    the (IMMEDIATE) transaction, so the table is rebuilt once."""
    import logging

    from cce.config.types import EvidenceStoreConfig
    from cce.evidence.sqlite import SQLiteEvidenceStore

    db_path = tmp_path / "legacy.db"
    await _seed_legacy_db(
        db_path,
        "PRAGMA journal_mode=WAL;\n" + _V3_DDL,
        [_legacy_row("ev_old000000001", "https://a.example/x", "E.")],
    )
    config = EvidenceStoreConfig(sqlite_path=db_path)
    first, late = SQLiteEvidenceStore(config), SQLiteEvidenceStore(config)
    real_check = late._has_hash_only_unique
    calls = 0

    async def stale_check() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            await first.connect()  # migrates while `late` is between steps
            return True
        return await real_check()

    late._has_hash_only_unique = stale_check  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="cce.evidence.sqlite"):
        await late.connect()
    try:
        rebuilds = [
            r for r in caplog.records if "Migrating evidence table" in r.message
        ]
        assert len(rebuilds) == 1
        assert not late._db.in_transaction
        assert await late.count() == 1
        assert await _unique_index_columns(late) == [("url", "excerpt_hash")]
    finally:
        await first.close()
        await late.close()
