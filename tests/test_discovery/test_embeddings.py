"""Tests for cce.discovery.embeddings — protocol types and cosine similarity."""

import pytest

from cce.discovery.discoverer import _cosine_similarity
from cce.discovery.embeddings import EmbeddingResult, EmbeddingUnavailableError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_vectors():
    vec = [1.0, 2.0, 3.0]
    assert _cosine_similarity(vec, vec) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors():
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]
    assert _cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector():
    a = [1.0, 2.0, 3.0]
    b = [0.0, 0.0, 0.0]
    assert _cosine_similarity(a, b) == 0.0


# ---------------------------------------------------------------------------
# EmbeddingResult / EmbeddingUnavailableError
# ---------------------------------------------------------------------------


def test_embedding_result_is_frozen():
    result = EmbeddingResult(vectors=[[1.0]], model="test", dimensions=1)
    with pytest.raises(AttributeError):
        result.model = "changed"


def test_embedding_unavailable_error_is_exception():
    assert issubclass(EmbeddingUnavailableError, Exception)
    err = EmbeddingUnavailableError("test")
    assert str(err) == "test"
