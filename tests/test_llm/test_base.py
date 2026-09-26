"""Tests for the B2 stop-reason contract in cce.llm.base."""

from __future__ import annotations

import pytest

from cce.llm.base import (
    IncompleteResponseError,
    LLMResponse,
    UnparseableResponseError,
    ensure_complete,
)
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


def test_unparseable_error_keeps_reply_off_the_message() -> None:
    response = LLMResponse(
        content="SENTINEL-CONFIDENTIAL garbage",
        model="claude-sonnet-5",
        stop_reason="end_turn",
    )

    error = UnparseableResponseError("writer", response)

    assert error.raw_response == "SENTINEL-CONFIDENTIAL garbage"
    assert error.role == "writer"
    assert error.stop_reason == "end_turn"
    assert error.model == "claude-sonnet-5"
    message = str(error)
    assert "writer" in message and "claude-sonnet-5" in message
    assert "SENTINEL-CONFIDENTIAL" not in message


def test_unparseable_error_is_retryable() -> None:
    """Unlike a truncated reply, an unparseable one is worth one resend."""
    assert issubclass(UnparseableResponseError, RETRYABLE_EXCEPTIONS)


def test_sum_usage_treats_a_null_count_as_zero():
    """The SDK types cache counts as Optional; a null must not crash the sum."""
    from cce.llm.base import sum_usage

    total = sum_usage(
        [
            {"input_tokens": 5, "cache_read_input_tokens": None},
            {"input_tokens": 7, "cache_read_input_tokens": 3},
        ]
    )
    assert total == {"input_tokens": 12, "cache_read_input_tokens": 3}
