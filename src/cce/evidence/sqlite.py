"""SQLite implementation of the evidence store.

Phase 1 storage backend. Uses aiosqlite for async compatibility with the
rest of the pipeline. The schema is intentionally simple -- one table, no
ORM, no migrations framework. If the schema needs to change, we add a
version check and ALTER TABLE statements in _ensure_schema(); a changed
constraint needs a table rebuild instead (see _migrate_to_v4).
"""

from __future__ import annotations

import json
import logging
import struct

import aiosqlite

from cce.config.types import EvidenceStoreConfig
from cce.models.evidence import Evidence, SourceQuality

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# Chunk size for WHERE url IN (...) lookups. Kept well below SQLite's default
# SQLITE_MAX_VARIABLE_NUMBER (999 on most builds) so callers can pass a large
# candidate list without hitting the parameter cap.
_URL_LOOKUP_CHUNK = 500

# Column order matters: _from_row reads SELECT * rows positionally, and the
# v4 rebuild copies by these names.
_EVIDENCE_COLUMNS = (
    "id",
    "url",
    "title",
    "author",
    "published_at",
    "retrieved_at",
    "excerpt",
    "excerpt_hash",
    "locator",
    "source_quality",
    "tags",
    "dimension_signals",
)

_EVIDENCE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    published_at    TEXT,       -- ISO 8601
    retrieved_at    TEXT NOT NULL, -- ISO 8601
    excerpt         TEXT NOT NULL,
    excerpt_hash    TEXT NOT NULL,
    locator         TEXT,
    source_quality  TEXT,       -- JSON blob, nullable
    tags            TEXT,       -- JSON array, nullable (v3)
    dimension_signals TEXT,     -- JSON object, nullable (v3)

    -- One row per verbatim excerpt per source URL (v4, B6). v1-v3 had
    -- UNIQUE(excerpt_hash): an excerpt syndicated at a second URL was
    -- silently not stored, and cited under an ID that never existed.
    UNIQUE(url, excerpt_hash)
);
"""

CREATE_EVIDENCE_TABLE = _EVIDENCE_TABLE_DDL.format(table="evidence")

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_evidence_url ON evidence(url);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(excerpt_hash);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_retrieved ON evidence(retrieved_at);",
    # TODO: tags is a JSON text blob — B-tree index won't help json_each() queries.
    # Add a junction table (evidence_tags) or functional index when tag-based queries are needed.
]

CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _serialize_float32(vec: list[float]) -> bytes:
    """Serialize a vector to the float32 binary format expected by sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


class SQLiteEvidenceStore:
    """Async SQLite-backed evidence store."""

    def __init__(self, config: EvidenceStoreConfig) -> None:
        self._db_path = config.sqlite_path
        self._db: aiosqlite.Connection | None = None
        self._vec_available: bool = False

    async def connect(self) -> None:
        """Open the database and ensure the schema exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")

        # Load sqlite-vec extension if available
        try:
            import sqlite_vec

            raw_conn = self._db._conn  # underlying sqlite3.Connection
            raw_conn.enable_load_extension(True)
            sqlite_vec.load(raw_conn)
            raw_conn.enable_load_extension(False)
            self._vec_available = True
        except (ImportError, Exception) as e:
            logger.warning("sqlite-vec not available: %s", e)
            self._vec_available = False

        await self._ensure_schema()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # -- EvidenceStore protocol --

    async def put(self, evidence: Evidence) -> bool:
        assert self._db is not None
        try:
            await self._db.execute(
                """
                INSERT INTO evidence
                    (id, url, title, author, published_at, retrieved_at,
                     excerpt, excerpt_hash, locator, source_quality,
                     tags, dimension_signals)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._to_row(evidence),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # duplicate (url, excerpt_hash) or id

    async def put_many(self, evidence: list[Evidence]) -> int:
        """Insert multiple evidence objects, skipping duplicates.

        A skipped row keeps its in-memory ID, which then names nothing in the
        store; callers resolve it with :meth:`get_stored_ids` (B6).
        """
        assert self._db is not None
        if not evidence:
            return 0

        rows = [self._to_row(ev) for ev in evidence]
        cursor = await self._db.executemany(
            """
            INSERT OR IGNORE INTO evidence
                (id, url, title, author, published_at, retrieved_at,
                 excerpt, excerpt_hash, locator, source_quality,
                 tags, dimension_signals)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._db.commit()
        return cursor.rowcount

    async def get(self, evidence_id: str) -> Evidence | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._from_row(row) if row else None

    async def get_many(self, evidence_ids: list[str]) -> list[Evidence]:
        assert self._db is not None
        if not evidence_ids:
            return []
        placeholders = ",".join("?" for _ in evidence_ids)
        async with self._db.execute(
            f"SELECT * FROM evidence WHERE id IN ({placeholders})",
            evidence_ids,
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._from_row(row) for row in rows]

    async def search(
        self,
        *,
        url: str | None = None,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[Evidence]:
        assert self._db is not None
        conditions: list[str] = []
        params: list[str] = []

        if url:
            conditions.append("url LIKE ?")
            params.append(f"{url}%")
        if topic:
            conditions.append("(title LIKE ? OR excerpt LIKE ?)")
            params.extend([f"%{topic}%", f"%{topic}%"])

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM evidence {where} ORDER BY retrieved_at DESC LIMIT ?"
        params.append(str(limit))

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._from_row(row) for row in rows]

    async def get_stored_ids(self, evidence: list[Evidence]) -> dict[str, str]:
        """Map in-memory IDs to the stored ID of the same (url, excerpt_hash).

        Returns only the IDs that differ: rows a concurrent job stored first
        under its own ID, so this job's copy was skipped by ``put_many``. Only
        same-URL, same-text rows are ever mapped, so provenance is unchanged.
        Chunked like :meth:`get_existing_urls`.
        """
        if not evidence:
            return {}
        assert self._db is not None
        hashes = sorted({ev.excerpt_hash for ev in evidence})
        stored: dict[tuple[str, str], str] = {}
        for start in range(0, len(hashes), _URL_LOOKUP_CHUNK):
            chunk = hashes[start : start + _URL_LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT id, url, excerpt_hash FROM evidence "
                f"WHERE excerpt_hash IN ({placeholders})"
            )
            async with self._db.execute(query, chunk) as cursor:
                for row_id, url, excerpt_hash in await cursor.fetchall():
                    stored[(url, excerpt_hash)] = row_id
        remap: dict[str, str] = {}
        for ev in evidence:
            stored_id = stored.get((ev.url, ev.excerpt_hash))
            if stored_id is not None and stored_id != ev.id:
                remap[ev.id] = stored_id
        return remap

    async def exists_by_hash(self, excerpt_hash: str) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM evidence WHERE excerpt_hash = ?", (excerpt_hash,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_existing_urls(self, candidates: list[str]) -> set[str]:
        """Return the subset of `candidates` already present in the evidence store.

        Chunked so a large candidate list stays under SQLite's parameter cap.
        """
        if not candidates:
            return set()
        assert self._db is not None
        found: set[str] = set()
        for start in range(0, len(candidates), _URL_LOOKUP_CHUNK):
            chunk = candidates[start : start + _URL_LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            query = f"SELECT DISTINCT url FROM evidence WHERE url IN ({placeholders})"
            async with self._db.execute(query, chunk) as cursor:
                rows = await cursor.fetchall()
            found.update(row[0] for row in rows)
        return found

    async def get_by_urls(self, urls: list[str]) -> list[Evidence]:
        """Return every stored Evidence row whose url is in the given list.

        Used by the discoverer to rehydrate evidence for URLs that were
        skipped by `get_existing_urls`-driven dedup. Chunked like
        `get_existing_urls` to stay under the SQLite parameter cap.
        """
        if not urls:
            return []
        assert self._db is not None
        evidence: list[Evidence] = []
        for start in range(0, len(urls), _URL_LOOKUP_CHUNK):
            chunk = urls[start : start + _URL_LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            # rowid order: first stored wins when two reused URLs carry the
            # same excerpt (the discoverer keeps the first copy per hash).
            query = (
                f"SELECT * FROM evidence WHERE url IN ({placeholders}) ORDER BY rowid"
            )
            async with self._db.execute(query, chunk) as cursor:
                rows = await cursor.fetchall()
            evidence.extend(self._from_row(row) for row in rows)
        return evidence

    async def count(self) -> int:
        assert self._db is not None
        async with self._db.execute("SELECT COUNT(*) FROM evidence") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    # -- Internal helpers --

    # -- Vector operations (sqlite-vec) --

    @property
    def vec_available(self) -> bool:
        """Whether sqlite-vec is loaded and the vector table exists."""
        return self._vec_available

    async def put_embedding(self, evidence_id: str, embedding: list[float]) -> bool:
        """Store an embedding vector for an evidence object."""
        if not self._vec_available:
            return False
        assert self._db is not None
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO evidence_vec (evidence_id, embedding) VALUES (?, ?)",
                (evidence_id, _serialize_float32(embedding)),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.warning("Failed to store embedding for %s: %s", evidence_id, e)
            return False

    async def search_by_embedding(
        self,
        query_embedding: list[float],
        *,
        k: int = 20,
    ) -> list[tuple[str, float]]:
        """KNN search against stored embeddings.

        Returns list of (evidence_id, distance) pairs, closest first.
        """
        if not self._vec_available:
            return []
        assert self._db is not None
        async with self._db.execute(
            """
            SELECT evidence_id, distance
            FROM evidence_vec
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (_serialize_float32(query_embedding), k),
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    # -- Internal helpers --

    async def _ensure_schema(self) -> None:
        assert self._db is not None
        await self._db.execute(CREATE_META_TABLE)
        await self._db.execute(CREATE_EVIDENCE_TABLE)
        for idx_sql in CREATE_INDEXES:
            await self._db.execute(idx_sql)

        # Add vector table if sqlite-vec is available
        if self._vec_available:
            await self._db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS evidence_vec USING vec0(
                    evidence_id TEXT PRIMARY KEY,
                    embedding FLOAT[768]
                );
            """)

        # Check stored version and run migrations if needed
        stored_version = 0
        async with self._db.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stored_version = int(row[0])

        if stored_version < 3:
            await self._migrate_to_v3()
        # By table shape, not version: an older binary opening a v4 file
        # rewrites schema_version to 3 but leaves the v4 table, and the
        # rebuild must neither repeat nor be skipped because of that.
        await self._migrate_to_v4()

        if stored_version != SCHEMA_VERSION:
            await self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

        await self._db.commit()

    async def _migrate_to_v3(self) -> None:
        """Add tags and dimension_signals columns if missing."""
        assert self._db is not None
        async with self._db.execute("PRAGMA table_info(evidence)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}

        if "tags" not in columns:
            await self._db.execute("ALTER TABLE evidence ADD COLUMN tags TEXT;")
        if "dimension_signals" not in columns:
            await self._db.execute(
                "ALTER TABLE evidence ADD COLUMN dimension_signals TEXT;"
            )

    async def _migrate_to_v4(self) -> None:
        """Rebuild the table if it still has v1-v3's UNIQUE(excerpt_hash) (B6).

        SQLite can't drop an inline constraint, so this copies every row into
        a table with UNIQUE(url, excerpt_hash) in one transaction (lossless:
        rows unique by hash are unique by (url, hash)), then re-creates the
        indexes the DROP removed. A no-op for a table already in v4 shape.
        """
        assert self._db is not None
        if not await self._has_hash_only_unique():
            return
        columns = ", ".join(_EVIDENCE_COLUMNS)
        logger.info("Migrating evidence table to v4: UNIQUE(url, excerpt_hash)")
        await self._db.commit()
        await self._db.executescript(
            "BEGIN;\n"
            "DROP TABLE IF EXISTS evidence_v4;\n"
            + _EVIDENCE_TABLE_DDL.format(table="evidence_v4")
            + f"INSERT INTO evidence_v4 ({columns}) SELECT {columns} FROM evidence;\n"
            "DROP TABLE evidence;\n"
            "ALTER TABLE evidence_v4 RENAME TO evidence;\n"
            "COMMIT;\n"
        )
        for idx_sql in CREATE_INDEXES:
            await self._db.execute(idx_sql)

    async def _has_hash_only_unique(self) -> bool:
        """True when a unique index covers exactly (excerpt_hash)."""
        assert self._db is not None
        async with self._db.execute("PRAGMA index_list(evidence)") as cursor:
            indexes = await cursor.fetchall()
        for index in indexes:
            name, unique = index[1], index[2]
            if not unique:
                continue
            async with self._db.execute(f"PRAGMA index_info('{name}')") as cursor:
                columns = [row[2] for row in await cursor.fetchall()]
            if columns == ["excerpt_hash"]:
                return True
        return False

    @staticmethod
    def _to_row(ev: Evidence) -> tuple:
        return (
            ev.id,
            ev.url,
            ev.title,
            ev.author,
            ev.published_at.isoformat() if ev.published_at else None,
            ev.retrieved_at.isoformat(),
            ev.excerpt,
            ev.excerpt_hash,
            ev.locator,
            ev.source_quality.model_dump_json() if ev.source_quality else None,
            json.dumps(ev.tags),
            json.dumps(ev.dimension_signals),
        )

    @staticmethod
    def _from_row(row: aiosqlite.Row | tuple) -> Evidence:
        from datetime import datetime

        source_quality = None
        if row[9]:
            source_quality = SourceQuality.model_validate_json(row[9])

        tags = json.loads(row[10]) if row[10] else []
        dimension_signals = json.loads(row[11]) if row[11] else {}

        return Evidence(
            id=row[0],
            url=row[1],
            title=row[2],
            author=row[3],
            published_at=datetime.fromisoformat(row[4]) if row[4] else None,
            retrieved_at=datetime.fromisoformat(row[5]),
            excerpt=row[6],
            excerpt_hash=row[7],
            locator=row[8],
            source_quality=source_quality,
            tags=tags,
            dimension_signals=dimension_signals,
        )
