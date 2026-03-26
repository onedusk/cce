"""Embedding provider protocol.

The discoverer uses this interface to embed evidence excerpts and topic queries
for semantic relevance ranking. Implementations must not be tied to a specific
model or API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of an embedding operation."""

    vectors: list[list[float]]
    model: str
    dimensions: int


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Interface for generating text embeddings."""

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed one or more texts and return their vector representations.

        Args:
            texts: List of text strings to embed.

        Returns:
            EmbeddingResult with one vector per input text, in order.

        Raises:
            EmbeddingUnavailableError: If the embedding service is unreachable.
        """
        ...


class EmbeddingUnavailableError(Exception):
    """Raised when the embedding service cannot be reached.

    Callers should catch this and fall back to non-embedding ranking.
    """
