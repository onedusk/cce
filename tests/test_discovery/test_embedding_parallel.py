"""Tests for Discoverer._embed_batches — parallel batch dispatch (audit P2, T-03.02)."""

from __future__ import annotations

import asyncio
import time

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import Discoverer
from cce.discovery.embeddings import EmbeddingResult

pytestmark = pytest.mark.integration


_PER_BATCH_LATENCY_S = 0.05


class _SleepyEmbedder:
    """Stub embedder that sleeps per call and returns one distinct vector per text."""

    def __init__(self, *, latency_s: float = _PER_BATCH_LATENCY_S) -> None:
        self._latency_s = latency_s
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        await asyncio.sleep(self._latency_s)
        # Vector per text: encode the text's index within the call so tests can
        # verify order preservation.
        vectors = [[float(len(t)), float(i)] for i, t in enumerate(texts)]
        return EmbeddingResult(vectors=vectors, model="mock", dimensions=2)


def _build_discoverer(embedder, *, batch_size: int, concurrency: int) -> Discoverer:
    return Discoverer(
        adapter=None,  # type: ignore[arg-type]  # unused by _embed_batches
        config=CrawlConfig(api_key="test"),
        embedding_provider=embedder,  # type: ignore[arg-type]  # structural typing
        embedding_batch_size=batch_size,
        embedding_concurrency=concurrency,
    )


async def test_empty_input_returns_empty():
    embedder = _SleepyEmbedder()
    discoverer = _build_discoverer(embedder, batch_size=10, concurrency=3)

    result = await discoverer._embed_batches([])

    assert result == []
    assert embedder.calls == []


async def test_output_order_matches_input():
    """3 batches of 10+10+5 texts — output order must match input text order."""
    embedder = _SleepyEmbedder()
    discoverer = _build_discoverer(embedder, batch_size=10, concurrency=3)
    texts = [f"text-{i:02d}" for i in range(25)]

    vectors = await discoverer._embed_batches(texts)

    assert len(vectors) == 25
    # The stub encodes each text's length + within-batch index — check lengths match
    # in input order.
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]


async def test_concurrency_1_is_sequential():
    """concurrency=1 → batches await each other; wall-clock ~= N * latency."""
    embedder = _SleepyEmbedder()
    discoverer = _build_discoverer(embedder, batch_size=10, concurrency=1)
    texts = [f"t{i}" for i in range(25)]  # 3 batches

    t0 = time.monotonic()
    await discoverer._embed_batches(texts)
    wall = time.monotonic() - t0

    # 3 batches * 0.05s each, sequential floor = 0.15s. Allow a small slop.
    assert wall >= 3 * _PER_BATCH_LATENCY_S * 0.9
    assert len(embedder.calls) == 3


async def test_concurrency_3_is_parallel():
    """concurrency=3 → 3 batches launch concurrently; wall-clock ~= 1 * latency."""
    embedder = _SleepyEmbedder()
    discoverer = _build_discoverer(embedder, batch_size=10, concurrency=3)
    texts = [f"t{i}" for i in range(25)]  # 3 batches

    t0 = time.monotonic()
    await discoverer._embed_batches(texts)
    wall = time.monotonic() - t0

    # Concurrent floor = 1 * 0.05s. Allow substantial slop for scheduling,
    # but must be well below sequential 0.15s.
    assert wall < 2 * _PER_BATCH_LATENCY_S, (
        f"Expected <{2 * _PER_BATCH_LATENCY_S}s; got {wall:.3f}s"
    )
    assert len(embedder.calls) == 3


async def test_batch_sizing_respects_batch_size():
    """batch_size=10 → 25 texts split into exactly [10, 10, 5]."""
    embedder = _SleepyEmbedder()
    discoverer = _build_discoverer(embedder, batch_size=10, concurrency=3)
    texts = [f"t{i}" for i in range(25)]

    await discoverer._embed_batches(texts)

    sizes = sorted(len(c) for c in embedder.calls)
    assert sizes == [5, 10, 10]


async def test_no_embedding_provider_returns_empty():
    """No embedder wired → returns [] without raising."""
    discoverer = Discoverer(
        adapter=None,  # type: ignore[arg-type]
        config=CrawlConfig(api_key="test"),
        embedding_provider=None,
        embedding_batch_size=10,
        embedding_concurrency=3,
    )

    result = await discoverer._embed_batches(["a", "b", "c"])
    assert result == []
