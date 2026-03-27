"""Taxonomy plugin protocol.

The TaxonomyPlugin interface allows swappable classification strategies.
Follows the same pattern as EmbeddingProvider: Protocol-based, optional,
graceful fallback on error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cce.models.evidence import Evidence


@dataclass(frozen=True)
class TaggingResult:
    """Result of tagging a single evidence object."""

    tags: list[str] = field(default_factory=list)
    signals: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0


@runtime_checkable
class TaxonomyPlugin(Protocol):
    """Interface for tagging evidence with taxonomy dimensions.

    Implementations receive evidence objects and return classification results.
    All methods are async. Errors should raise TaxonomyUnavailableError
    to signal graceful fallback.
    """

    async def tag(self, evidence: Evidence) -> TaggingResult:
        """Tag a single evidence object.

        Args:
            evidence: The evidence to classify.

        Returns:
            TaggingResult with tags, dimension signals, and confidence.

        Raises:
            TaxonomyUnavailableError: If classification fails.
        """
        ...

    async def tag_many(self, evidence: list[Evidence]) -> list[TaggingResult]:
        """Tag multiple evidence objects.

        Implementations must return results in the same order as input.
        For CPU-bound classifiers, sequential iteration is fine.
        For I/O-bound classifiers (e.g., LLM), consider batching.

        Args:
            evidence: Evidence objects to classify.

        Returns:
            TaggingResults in the same order as input evidence.
        """
        ...


class TaxonomyUnavailableError(Exception):
    """Raised when taxonomy classification fails.

    Callers should catch this and proceed without tags.
    """
