"""Tests for the B2 stop-reason contract in cce.llm.base."""

from __future__ import annotations

import pytest

from cce.llm.base import IncompleteResponseError, LLMResponse, ensure_complete
from cce.llm.retry import RETRYABLE_EXCEPTIONS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", ""])
def test_ensure_complete_accepts_finished_replies(stop_reason: str) -> None:
    ensure_complete(LLMResponse(content="{}", stop_reason=stop_reason), role="writer")


def test_max_tokens_error_names_role_model_and_spend() -> None:
    response = LLMResponse(
        content='{"content": "cut off mid',
        model="claude-sonnet-5",
        usage={"output_tokens": 16384},
        stop_reason="max_tokens",
    )

    with pytest.raises(IncompleteResponseError) as exc:
        ensure_complete(response, role="writer")

    message = str(exc.value)
    assert "writer" in message
    assert "claude-sonnet-5" in message
    assert "max_tokens" in message
    assert "16384" in message
    assert (
        "CCE_LLM_EFFORT" in message
    )  # the lever left once max_tokens is at the ceiling
    assert exc.value.role == "writer"
    assert exc.value.stop_reason == "max_tokens"
    assert exc.value.model == "claude-sonnet-5"


def test_refusal_is_incomplete() -> None:
    response = LLMResponse(content="", model="claude-opus-5", stop_reason="refusal")

    with pytest.raises(IncompleteResponseError, match="stop_reason='refusal'"):
        ensure_complete(response, role="verifier")


def test_incomplete_error_is_not_retryable() -> None:
    """with_llm_retry resends on RETRYABLE_EXCEPTIONS; a truncated reply must
    not be resent with the same budget."""
    assert not issubclass(IncompleteResponseError, RETRYABLE_EXCEPTIONS)
