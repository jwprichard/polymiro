"""Async retry helper with exponential backoff and jitter.

Used by REST market-seeding functions in loop.py and polymarket_ws.py to
handle transient HTTP/network failures without crashing the pipeline.

Backoff parameters are module-level constants — operational tuning knobs,
not strategy parameters, so they live here rather than in config.py.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

from common.log import ulog

# ---------------------------------------------------------------------------
# Backoff constants
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BASE_DELAY_S: float = 2.0
RETRY_MAX_DELAY_S: float = 15.0

T = TypeVar("T")


def _backoff_delay(attempt: int) -> float:
    """Compute exponential backoff with jitter.

    ``delay = min(base * 2^(attempt-1) + jitter, max_delay)``

    *attempt* is 1-based (first retry = 1).
    """
    exp_delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
    jitter = random.uniform(0, RETRY_BASE_DELAY_S)
    return min(exp_delay + jitter, RETRY_MAX_DELAY_S)


async def retry_async(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    description: str = "operation",
    max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> T:
    """Execute *coro_fn* with up to *max_attempts* retries on failure.

    Parameters
    ----------
    coro_fn:
        A zero-argument callable that returns an awaitable.  Called fresh
        on each attempt.
    description:
        Human-readable label for log messages (e.g. ``"gamma market fetch"``).
    max_attempts:
        Total number of attempts (including the first).

    Returns
    -------
    The return value of *coro_fn* on success.

    Raises
    ------
    Exception
        The exception from the final failed attempt, after all retries
        are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = _backoff_delay(attempt)
                ulog.retry.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    description,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                ulog.retry.error(
                    "%s failed after %d attempts: %s",
                    description,
                    max_attempts,
                    exc,
                )

    # All attempts exhausted — re-raise the last exception.
    raise last_exc  # type: ignore[misc]
