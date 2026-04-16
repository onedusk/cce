"""Tests for cce.llm.retry — async retry wrapper for LLM operations."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from cce.llm.retry import (
    JITTER_FRACTION,
    RETRYABLE_EXCEPTIONS,
    _with_jitter,
    with_llm_retry,
)


@pytest.fixture
def no_jitter():
    """Zero out the jitter so tests that assert exact delay values remain stable."""
    with patch("cce.llm.retry.random.random", return_value=0.0):
        yield


# ---------------------------------------------------------------------------
# Success on first try
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_success_on_first_try():
    fn = AsyncMock(return_value="ok")
    result = await with_llm_retry(fn)
    assert result == "ok"
    fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# Success after one failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_success_after_one_failure(no_jitter):
    fn = AsyncMock(side_effect=[ValueError("bad json"), "ok"])
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_llm_retry(fn)
    assert result == "ok"
    assert fn.await_count == 2
    mock_sleep.assert_awaited_once_with(1.0)


# ---------------------------------------------------------------------------
# Exhausts all attempts
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_exhausts_all_attempts():
    fn = AsyncMock(side_effect=ValueError("bad json"))
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ValueError, match="bad json"):
            await with_llm_retry(fn, max_attempts=3)
    assert fn.await_count == 3


# ---------------------------------------------------------------------------
# Non-retryable exception propagates immediately
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_non_retryable_exception_propagates_immediately():
    fn = AsyncMock(side_effect=RuntimeError("network down"))
    with pytest.raises(RuntimeError, match="network down"):
        await with_llm_retry(fn)
    fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# Backoff timing
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_backoff_timing(no_jitter):
    fn = AsyncMock(side_effect=[ValueError("e1"), ValueError("e2"), "ok"])
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_llm_retry(fn, max_attempts=3)
    assert result == "ok"
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(1.0)  # attempt 1 -> delay = 1.0 * 2^0 * (1 + 0)
    mock_sleep.assert_any_await(2.0)  # attempt 2 -> delay = 1.0 * 2^1 * (1 + 0)


# ---------------------------------------------------------------------------
# Custom max_attempts
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_custom_max_attempts():
    fn = AsyncMock(side_effect=KeyError("missing"))
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KeyError):
            await with_llm_retry(fn, max_attempts=5)
    assert fn.await_count == 5


# ---------------------------------------------------------------------------
# KeyError is retryable
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_key_error_is_retryable():
    fn = AsyncMock(side_effect=[KeyError("missing key"), "ok"])
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await with_llm_retry(fn)
    assert result == "ok"
    assert fn.await_count == 2


# ---------------------------------------------------------------------------
# Jitter (audit P5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jitter_bounds_zero_factor():
    """At random()=0 the jitter multiplier is 1.0 — base delay unchanged."""
    with patch("cce.llm.retry.random.random", return_value=0.0):
        assert _with_jitter(4.0) == 4.0


@pytest.mark.unit
def test_jitter_bounds_max_factor():
    """At random()=1 the jitter multiplier is 1 + JITTER_FRACTION."""
    with patch("cce.llm.retry.random.random", return_value=1.0):
        assert _with_jitter(4.0) == pytest.approx(4.0 * (1.0 + JITTER_FRACTION))


@pytest.mark.unit
async def test_jitter_applied_in_backoff():
    """The delay passed to asyncio.sleep is >= base and <= base*(1+JITTER_FRACTION)."""
    fn = AsyncMock(side_effect=[ValueError("boom"), "ok"])
    with patch("cce.llm.retry.random.random", return_value=0.5):
        with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_llm_retry(fn)
    assert result == "ok"
    # Base delay at attempt 1 is 1.0; with random()=0.5 jitter is 1.0 * (1 + 0.5*0.25) = 1.125
    mock_sleep.assert_awaited_once_with(pytest.approx(1.125))


# ---------------------------------------------------------------------------
# json.JSONDecodeError explicit in retryable set (audit P5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jsondecodeerror_in_retryable_exceptions():
    assert json.JSONDecodeError in RETRYABLE_EXCEPTIONS


@pytest.mark.unit
async def test_jsondecodeerror_is_retried(no_jitter):
    err = json.JSONDecodeError("bad", "doc", 0)
    fn = AsyncMock(side_effect=[err, "ok"])
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await with_llm_retry(fn)
    assert result == "ok"
    assert fn.await_count == 2


# ---------------------------------------------------------------------------
# No type-ignore on final raise (audit P5, code-hygiene)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_raises_last_error_with_max_attempts_1():
    """max_attempts=1 exercises the no-retry branch where last_error must still raise."""
    fn = AsyncMock(side_effect=ValueError("once"))
    with pytest.raises(ValueError, match="once"):
        await with_llm_retry(fn, max_attempts=1)
    fn.assert_awaited_once()
