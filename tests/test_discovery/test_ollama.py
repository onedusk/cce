"""Tests for cce.discovery.ollama — OllamaEmbeddingProvider with mocked HTTP."""

import json

import httpx
import pytest

from cce.config.types import EmbeddingConfig
from cce.discovery.embeddings import EmbeddingUnavailableError
from cce.discovery.ollama import OllamaEmbeddingProvider

pytestmark = pytest.mark.unit


def _mock_transport(handler):
    """Create an httpx mock transport from a handler function."""
    return httpx.MockTransport(handler)


def _make_provider(transport, **config_overrides) -> OllamaEmbeddingProvider:
    config = EmbeddingConfig(
        model="test-model",
        dimensions=4,
        base_url="http://test-ollama:11434",
        batch_size=config_overrides.pop("batch_size", 64),
        **config_overrides,
    )
    provider = OllamaEmbeddingProvider(config)
    # Replace the internal client with one using our mock transport
    provider._client = httpx.AsyncClient(transport=transport, timeout=30)
    return provider


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


async def test_ollama_embed_single_text():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["input"] == ["hello world"]
        return httpx.Response(
            200,
            json={"embeddings": [[0.1, 0.2, 0.3, 0.4]]},
        )

    provider = _make_provider(_mock_transport(handler))
    result = await provider.embed(["hello world"])
    assert len(result.vectors) == 1
    assert result.vectors[0] == [0.1, 0.2, 0.3, 0.4]
    assert result.model == "test-model"
    assert result.dimensions == 4


async def test_ollama_embed_batch():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"])
        return httpx.Response(
            200,
            json={"embeddings": [[float(i)] * 4 for i in range(n)]},
        )

    provider = _make_provider(_mock_transport(handler))
    result = await provider.embed(["a", "b", "c"])
    assert len(result.vectors) == 3


async def test_ollama_embed_batching_splits():
    """Verify batch_size=2 splits 3 texts into 2 requests."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        n = len(body["input"])
        return httpx.Response(
            200,
            json={"embeddings": [[1.0] * 4 for _ in range(n)]},
        )

    provider = _make_provider(_mock_transport(handler), batch_size=2)
    result = await provider.embed(["a", "b", "c"])
    assert len(result.vectors) == 3
    assert call_count == 2  # batch 1: [a,b], batch 2: [c]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


async def test_ollama_embed_http_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    provider = _make_provider(_mock_transport(handler))
    with pytest.raises(EmbeddingUnavailableError, match="request failed"):
        await provider.embed(["test"])


async def test_ollama_embed_connection_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    provider = _make_provider(_mock_transport(handler))
    with pytest.raises(EmbeddingUnavailableError, match="request failed"):
        await provider.embed(["test"])


async def test_ollama_embed_wrong_count_raises_unavailable():
    """Server returns fewer embeddings than inputs."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"embeddings": [[1.0] * 4]},  # only 1, but we sent 2
        )

    provider = _make_provider(_mock_transport(handler))
    with pytest.raises(EmbeddingUnavailableError, match="Expected 2"):
        await provider.embed(["a", "b"])
