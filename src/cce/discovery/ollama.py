"""Ollama embedding adapter.

Uses the Ollama HTTP API for local embedding generation.
Phase 2 default embedding provider.
"""

from __future__ import annotations

import logging

import httpx

from cce.config.types import EmbeddingConfig
from cce.discovery.embeddings import EmbeddingResult, EmbeddingUnavailableError

logger = logging.getLogger(__name__)


class OllamaEmbeddingProvider:
    """Async Ollama embedding client."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._url = f"{config.base_url.rstrip('/')}/api/embed"
        self._client = httpx.AsyncClient(timeout=config.timeout_seconds)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed texts via Ollama's /api/embed endpoint.

        Batches requests according to config.batch_size.
        """
        all_vectors: list[list[float]] = []

        for i in range(0, len(texts), self._config.batch_size):
            batch = texts[i : i + self._config.batch_size]
            try:
                response = await self._client.post(
                    self._url,
                    json={"model": self._config.model, "input": batch},
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("embeddings", [])
                if len(embeddings) != len(batch):
                    raise EmbeddingUnavailableError(
                        f"Expected {len(batch)} embeddings, got {len(embeddings)}"
                    )
                all_vectors.extend(embeddings)
            except httpx.HTTPError as e:
                raise EmbeddingUnavailableError(
                    f"Ollama embedding request failed: {e}"
                ) from e
            except (KeyError, ValueError) as e:
                raise EmbeddingUnavailableError(
                    f"Ollama embedding response parsing failed: {e}"
                ) from e

        return EmbeddingResult(
            vectors=all_vectors,
            model=self._config.model,
            dimensions=self._config.dimensions,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
