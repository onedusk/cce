"""SQLite implementation of the evidence store.

Phase 1 storage backend. Uses aiosqlite for async compatibility with the
rest of the pipeline. The schema is intentionally simple -- one table, no
ORM, no migrations framework. If the schema needs to change, we add a
version check and ALTER TABLE statements in _ensure_schema().
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from cce.config.types import EvidenceStoreConfig
from cce.models.evidence import Evidence, SourceQuality

SCHEMA_VERSION = 1

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

    UNIQUE(excerpt_hash)       -- dedup on verbatim content
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_evidence_url ON evidence(url);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(excerpt_hash);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_retrieved ON evidence(retrieved_at);",
]

CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SQLiteEvidenceStore:
    """Async SQLite-backed evidence store."""

    def __init__(self, config: EvidenceStoreConfig) -> None:
        self._db_path = config.sqlite_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and ensure the schema exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
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
                     excerpt, excerpt_hash, locator, source_quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._to_row(evidence),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # duplicate excerpt_hash

    async def put_many(self, evidence: list[Evidence]) -> int:
        assert self._db is not None
        inserted = 0
        for ev in evidence:
            try:
                await self._db.execute(
                    """
                    INSERT INTO evidence
                        (id, url, title, author, published_at, retrieved_at,
                         excerpt, excerpt_hash, locator, source_quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._to_row(ev),
                )
                inserted += 1
            except aiosqlite.IntegrityError:
                continue  # skip duplicates
        await self._db.commit()
        return inserted

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

    async def count(self) -> int:
        assert self._db is not None
        async with self._db.execute("SELECT COUNT(*) FROM evidence") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    # -- Internal helpers --

    async def _ensure_schema(self) -> None:
        assert self._db is not None
        await self._db.execute(CREATE_META_TABLE)
        await self._db.execute(CREATE_EVIDENCE_TABLE)
        for idx_sql in CREATE_INDEXES:
            await self._db.execute(idx_sql)

        # Store schema version for future migrations
        await self._db.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        await self._db.commit()

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
        )

    @staticmethod
    def _from_row(row: tuple) -> Evidence:
        from datetime import datetime

        source_quality = None
        if row[9]:
            source_quality = SourceQuality.model_validate_json(row[9])

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
        )
