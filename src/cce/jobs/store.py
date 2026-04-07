"""SQLite-backed job and package persistence.

Shares the same database file as the evidence store. Opens its own
connection. Schema is version-managed independently via the
``jobs_schema_version`` key in the shared ``_meta`` table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from cce.models.job import Job, JobStatus
from cce.models.package import PublishPackage

logger = logging.getLogger(__name__)

JOB_SCHEMA_VERSION = 1

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    topic           TEXT NOT NULL,
    policy_id       TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    job_json        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);
"""

CREATE_PACKAGES_TABLE = """
CREATE TABLE IF NOT EXISTS packages (
    job_id          TEXT PRIMARY KEY,
    package_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""

CREATE_API_KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash        TEXT PRIMARY KEY,
    label           TEXT,
    created_at      TEXT NOT NULL
);
"""

CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CREATE_JOB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_topic ON jobs(topic);",
]


class JobStore:
    """Async SQLite-backed store for jobs, packages, and API keys."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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

    async def ping(self) -> bool:
        """Return True if the database is reachable."""
        if self._db is None:
            return False
        try:
            async with self._db.execute("SELECT 1") as cursor:
                await cursor.fetchone()
            return True
        except Exception:
            return False

    # -- Job CRUD --

    async def create_job(self, job: Job) -> None:
        """Insert a new job."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO jobs
                (id, status, topic, policy_id, request_json,
                 job_json, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.status.value,
                job.request.topic,
                job.request.policy_id,
                job.request.model_dump_json(),
                job.model_dump_json(),
                job.created_at.isoformat(),
                now,
                job.completed_at.isoformat() if job.completed_at else None,
            ),
        )
        await self._db.commit()

    async def get_job(self, job_id: str) -> Job | None:
        """Fetch a job by ID, or None if not found."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT job_json FROM jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return Job.model_validate_json(row[0])

    async def update_job(self, job: Job) -> None:
        """Update an existing job (status, timestamps, error, stages)."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, job_json = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                job.status.value,
                job.model_dump_json(),
                now,
                job.completed_at.isoformat() if job.completed_at else None,
                job.id,
            ),
        )
        await self._db.commit()

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        topic: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List jobs with optional filters and pagination."""
        assert self._db is not None
        clauses: list[str] = []
        params: list[str | int] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT job_json FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [Job.model_validate_json(row[0]) for row in rows]

    async def count_jobs(
        self,
        *,
        status: JobStatus | None = None,
        topic: str | None = None,
    ) -> int:
        """Count jobs matching the given filters."""
        assert self._db is not None
        clauses: list[str] = []
        params: list[str] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._db.execute(
            f"SELECT COUNT(*) FROM jobs {where}", params
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job and its associated package. Returns False if not found."""
        assert self._db is not None
        await self._db.execute("DELETE FROM packages WHERE job_id = ?", (job_id,))
        cursor = await self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    # -- Package CRUD --

    async def store_package(self, job_id: str, package: PublishPackage) -> None:
        """Store a completed pipeline output."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO packages (job_id, package_json, created_at)
            VALUES (?, ?, ?)
            """,
            (job_id, package.model_dump_json(), now),
        )
        await self._db.commit()

    async def get_package(self, job_id: str) -> PublishPackage | None:
        """Fetch the package for a job, or None if not found."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT package_json FROM packages WHERE job_id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return PublishPackage.model_validate_json(row[0])

    # -- API Key CRUD --

    async def store_api_key(self, key_hash: str, label: str | None = None) -> None:
        """Store a hashed API key."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO api_keys (key_hash, label, created_at) VALUES (?, ?, ?)",
            (key_hash, label, now),
        )
        await self._db.commit()

    async def verify_api_key(self, key_hash: str) -> bool:
        """Check if a hashed API key exists."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM api_keys WHERE key_hash = ?", (key_hash,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def list_api_keys(self) -> list[dict]:
        """List all API keys (hash, label, created_at)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT key_hash, label, created_at FROM api_keys ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"key_hash": row[0], "label": row[1], "created_at": row[2]}
                for row in rows
            ]

    async def delete_api_key(self, key_hash: str) -> bool:
        """Delete an API key. Returns False if not found."""
        assert self._db is not None
        cursor = await self._db.execute(
            "DELETE FROM api_keys WHERE key_hash = ?", (key_hash,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # -- Schema --

    async def _ensure_schema(self) -> None:
        """Create tables, indexes, and run migrations if needed."""
        assert self._db is not None

        await self._db.execute(CREATE_META_TABLE)
        await self._db.execute(CREATE_JOBS_TABLE)
        await self._db.execute(CREATE_PACKAGES_TABLE)
        await self._db.execute(CREATE_API_KEYS_TABLE)

        for idx_sql in CREATE_JOB_INDEXES:
            await self._db.execute(idx_sql)

        # Check stored version — namespaced to avoid collision with evidence store
        stored_version = 0
        async with self._db.execute(
            "SELECT value FROM _meta WHERE key = 'jobs_schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stored_version = int(row[0])

        if stored_version < JOB_SCHEMA_VERSION:
            # Future migrations go here: if stored_version < 2: await self._migrate_to_v2()
            await self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("jobs_schema_version", str(JOB_SCHEMA_VERSION)),
            )

        await self._db.commit()
