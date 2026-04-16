"""Async retry wrapper for LLM operations."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_ATTEMPTS = 3
BASE_DELAY_S = 1.0
JITTER_FRACTION = 0.25  # Max +fraction of the current exponential delay.

# JSONDecodeError is a subclass of ValueError, but listed explicitly so that
# a future refactor narrowing ValueError cannot silently drop JSON-parse retries.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ValueError,
    KeyError,
    json.JSONDecodeError,
)


def _with_jitter(delay: float) -> float:
    """Spread retry delays by up to JITTER_FRACTION to avoid lockstep retries."""
    return delay * (1.0 + random.random() * JITTER_FRACTION)


async def with_llm_retry(
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY_S,
    **kwargs: object,
) -> T:
    """Call an async function with retry on application-level failures.

    Retries on ValueError, KeyError, and json.JSONDecodeError (all JSON/shape
    problems surfaced by the adapter layer). Does NOT retry on network/SDK
    errors — the SDK's own retries handle those.

    Backoff is exponential with jitter: attempt N waits
    ``base_delay * 2^(N-1) * (1 + rand()*JITTER_FRACTION)`` seconds.
    """
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as e:
            last_error = e
            if attempt < max_attempts:
                delay = _with_jitter(base_delay * (2 ** (attempt - 1)))
                logger.warning(
                    "LLM operation failed (attempt %d/%d): %s. Retrying in %.2fs",
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
    assert last_error is not None
    raise last_error
