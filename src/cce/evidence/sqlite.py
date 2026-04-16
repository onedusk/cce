"""SQLite implementation of the evidence store.

Phase 1 storage backend. Uses aiosqlite for async compatibility with the
rest of the pipeline. The schema is intentionally simple -- one table, no
ORM, no migrations framework. If the schema needs to change, we add a
version check and ALTER TABLE statements in _ensure_schema().
"""

from __future__ import annotations

import json
import logging
import struct

import aiosqlite

from cce.config.types import EvidenceStoreConfig
from cce.models.evidence import Evidence, SourceQuality

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# Chunk size for WHERE url IN (...) lookups. Kept well below SQLite's default
# SQLITE_MAX_VARIABLE_NUMBER (999 on most builds) so callers can pass a large
# candidate list without hitting the parameter cap.
_URL_LOOKUP_CHUNK = 500

CREATE_EVIDENCE_TABLE = """
CREATE TABLE IF NOT EXISTS evidence (
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

    UNIQUE(excerpt_hash)       -- dedup on verbatim content
);
"""

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
            return False  # duplicate excerpt_hash

    async def put_many(self, evidence: list[Evidence]) -> int:
        """Insert multiple evidence objects, skipping duplicates."""
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
    def _from_row(row: tuple) -> Evidence:
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
