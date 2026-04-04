"""Tests for cce.llm.retry — async retry wrapper for LLM operations."""

from unittest.mock import AsyncMock, patch

import pytest

from cce.llm.retry import with_llm_retry


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
async def test_success_after_one_failure():
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
async def test_backoff_timing():
    fn = AsyncMock(side_effect=[ValueError("e1"), ValueError("e2"), "ok"])
    with patch("cce.llm.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_llm_retry(fn, max_attempts=3)
    assert result == "ok"
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(1.0)   # attempt 1 -> delay = 1.0 * 2^0
    mock_sleep.assert_any_await(2.0)   # attempt 2 -> delay = 1.0 * 2^1


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
