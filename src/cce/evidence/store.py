"""Evidence store protocol.

Any storage backend implements this protocol. The rest of the engine
interacts with evidence exclusively through these methods.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cce.models.evidence import Evidence


@runtime_checkable
class EvidenceStore(Protocol):
    """Interface for persisting and retrieving evidence objects."""

    async def put(self, evidence: Evidence) -> bool:
        """Store an evidence object. Returns False if it was a duplicate (by excerpt_hash)."""
        ...

    async def put_many(self, evidence: list[Evidence]) -> int:
        """Store multiple evidence objects. Returns count of newly inserted (non-duplicate)."""
        ...

    async def get(self, evidence_id: str) -> Evidence | None:
        """Retrieve a single evidence object by ID."""
        ...

    async def get_many(self, evidence_ids: list[str]) -> list[Evidence]:
        """Retrieve multiple evidence objects by ID. Missing IDs are silently skipped."""
        ...

    async def search(
        self,
        *,
        url: str | None = None,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[Evidence]:
        """Search evidence by URL prefix or topic keyword. Used for dedup checks."""
        ...

    async def exists_by_hash(self, excerpt_hash: str) -> bool:
        """Check if evidence with this excerpt hash already exists."""
        ...

    async def count(self) -> int:
        """Total number of evidence objects in the store."""
        ...
