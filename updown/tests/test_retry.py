"""Tests for updown.retry — async retry with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, patch

import pytest

from updown.retry import (
    RETRY_BASE_DELAY_S,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY_S,
    _backoff_delay,
    retry_async,
)


# ---------------------------------------------------------------------------
# _backoff_delay tests
# ---------------------------------------------------------------------------


class TestBackoffDelay:
    """Tests for the _backoff_delay() helper."""

    def test_exponential_growth(self):
        """Delay grows exponentially with attempt number (ignoring jitter)."""
        random.seed(0)
        d1 = _backoff_delay(1)
        random.seed(0)
        d2 = _backoff_delay(2)
        random.seed(0)
        d3 = _backoff_delay(3)

        # With identical jitter (same seed), the exponential part doubles.
        # attempt 1: base * 2^0 = 2.0
        # attempt 2: base * 2^1 = 4.0
        # attempt 3: base * 2^2 = 8.0
        # All have the same jitter added, so d2 - d1 == 2.0, d3 - d2 == 4.0
        assert d2 - d1 == pytest.approx(2.0)
        assert d3 - d2 == pytest.approx(4.0)

    def test_capped_at_max_delay(self):
        """No matter how high the attempt number, delay never exceeds RETRY_MAX_DELAY_S."""
        random.seed(42)
        for attempt in range(1, 20):
            assert _backoff_delay(attempt) <= RETRY_MAX_DELAY_S

    def test_includes_jitter(self):
        """Different random seeds produce different delays for the same attempt."""
        random.seed(0)
        d_a = _backoff_delay(1)
        random.seed(999)
        d_b = _backoff_delay(1)
        # The exponential part is the same (2.0); only jitter differs.
        assert d_a != d_b

    def test_jitter_range(self):
        """Jitter is in [0, RETRY_BASE_DELAY_S), so total delay >= base * 2^(attempt-1)."""
        random.seed(12345)
        for attempt in range(1, 6):
            delay = _backoff_delay(attempt)
            exp_part = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            # delay >= exp_part (jitter >= 0)
            assert delay >= exp_part or delay == RETRY_MAX_DELAY_S
            # delay < exp_part + base (jitter < base) or capped
            assert delay <= min(exp_part + RETRY_BASE_DELAY_S, RETRY_MAX_DELAY_S)

    def test_deterministic_with_seed(self):
        """Same seed produces identical delay."""
        random.seed(77)
        d1 = _backoff_delay(2)
        random.seed(77)
        d2 = _backoff_delay(2)
        assert d1 == d2


# ---------------------------------------------------------------------------
# retry_async tests
# ---------------------------------------------------------------------------


class TestRetryAsync:
    """Tests for the retry_async() coroutine."""

    @pytest.mark.asyncio
    async def test_succeeds_first_attempt(self):
        """When the coroutine succeeds immediately, no retry or sleep occurs."""
        coro_fn = AsyncMock(return_value="ok")

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_async(coro_fn, description="test op")

        assert result == "ok"
        coro_fn.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_succeeds_on_retry_after_transient_failure(self):
        """Fails once, then succeeds on the second attempt."""
        coro_fn = AsyncMock(side_effect=[RuntimeError("boom"), "recovered"])

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_async(coro_fn, description="flaky op")

        assert result == "recovered"
        assert coro_fn.await_count == 2
        mock_sleep.assert_awaited_once()  # slept once between attempt 1 and 2

    @pytest.mark.asyncio
    async def test_exhausts_all_retries_and_reraises(self):
        """After max_attempts failures, the last exception is re-raised."""
        exc = ValueError("persistent failure")
        coro_fn = AsyncMock(side_effect=exc)

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ValueError, match="persistent failure"):
                await retry_async(coro_fn, description="doomed op")

        assert coro_fn.await_count == RETRY_MAX_ATTEMPTS
        # Sleep is called between attempts, so (max_attempts - 1) times.
        assert mock_sleep.await_count == RETRY_MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_max_attempts_one_no_retry(self):
        """With max_attempts=1, failure raises immediately with no sleep."""
        coro_fn = AsyncMock(side_effect=RuntimeError("instant fail"))

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RuntimeError, match="instant fail"):
                await retry_async(coro_fn, max_attempts=1, description="one-shot")

        coro_fn.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_value_from_coro_fn(self):
        """The return value of the successful coroutine is passed through."""
        expected = {"data": [1, 2, 3]}
        coro_fn = AsyncMock(return_value=expected)

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_async(coro_fn)

        assert result is expected

    @pytest.mark.asyncio
    async def test_reraises_last_exception_not_first(self):
        """When different exceptions occur, the *last* one is re-raised."""
        coro_fn = AsyncMock(
            side_effect=[
                TypeError("first"),
                ValueError("second"),
                RuntimeError("third"),
            ]
        )

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="third"):
                await retry_async(coro_fn, max_attempts=3)

    @pytest.mark.asyncio
    async def test_sleep_receives_backoff_delay(self):
        """asyncio.sleep is called with the value from _backoff_delay."""
        coro_fn = AsyncMock(side_effect=[OSError("fail"), "ok"])

        with (
            patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("updown.retry._backoff_delay", return_value=5.5) as mock_delay,
        ):
            await retry_async(coro_fn, description="delay check")

        mock_delay.assert_called_once_with(1)  # first retry = attempt 1
        mock_sleep.assert_awaited_once_with(5.5)

    @pytest.mark.asyncio
    async def test_custom_max_attempts(self):
        """max_attempts=5 allows up to 5 tries."""
        coro_fn = AsyncMock(
            side_effect=[
                RuntimeError("1"),
                RuntimeError("2"),
                RuntimeError("3"),
                RuntimeError("4"),
                "finally",
            ]
        )

        with patch("updown.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_async(coro_fn, max_attempts=5)

        assert result == "finally"
        assert coro_fn.await_count == 5
        assert mock_sleep.await_count == 4
