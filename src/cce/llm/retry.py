"""Async retry wrapper for LLM operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_ATTEMPTS = 3
BASE_DELAY_S = 1.0
RETRYABLE_EXCEPTIONS = (ValueError, KeyError)


async def with_llm_retry(
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY_S,
    **kwargs: object,
) -> T:
    """Call an async function with retry on application-level failures.

    Retries on ValueError and KeyError (JSON parse failures).
    Does NOT retry on network/SDK errors.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as e:
            last_error = e
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "LLM operation failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "LLM operation failed after %d attempts: %s", max_attempts, e
                )
    raise last_error  # type: ignore[misc]
