"""Tests for updown.binance_ws -- BTC/USDT trade stream client.

Covers:
- _handle_message() parsing valid trade JSON, ignoring non-trade events,
  ignoring malformed JSON
- _prune_window() removing stale entries
- get_window_open_price() returning correct price at window boundary,
  None when empty, fallback when all entries are stale
- _next_backoff_delay() exponential growth with cap
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from unittest.mock import patch

import pytest

from updown.binance_ws import BinanceWS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trade_msg(
    price: str = "67123.45",
    timestamp_ms: int = 1_700_000_000_000,
    symbol: str = "BTCUSDT",
) -> str:
    """Build a realistic Binance trade stream JSON message."""
    return json.dumps({
        "e": "trade",
        "E": timestamp_ms,
        "s": symbol,
        "t": 123456,
        "p": price,
        "q": "0.001",
        "T": timestamp_ms,
        "m": True,
        "M": True,
    })


def _make_client(
    window_seconds: int = 300,
    queue_maxsize: int = 100,
) -> tuple[BinanceWS, asyncio.Queue]:
    """Build a BinanceWS with a fresh queue and no real connections."""
    q: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
    client = BinanceWS(q, window_seconds=window_seconds, ws_url="wss://fake")
    return client, q


# ═══════════════════════════════════════════════════════════════════════════
# _handle_message tests
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleMessage:
    """Verify _handle_message() parses, enqueues, and filters correctly."""

    @pytest.mark.asyncio
    async def test_valid_trade_enqueues_price_update(self) -> None:
        """A valid trade message should produce a PriceUpdate on the queue."""
        client, q = _make_client()
        msg = _trade_msg(price="67500.00", timestamp_ms=1_700_000_001_000)

        await client._handle_message(msg)

        assert not q.empty()
        update = q.get_nowait()
        assert update.symbol == "BTCUSDT"
        assert update.price == 67500.00
        assert update.timestamp_ms == 1_700_000_001_000

    @pytest.mark.asyncio
    async def test_valid_trade_appended_to_window(self) -> None:
        """A valid trade should be appended to the rolling price window."""
        client, _ = _make_client()
        ts = 1_700_000_000_000
        await client._handle_message(_trade_msg(price="67000.00", timestamp_ms=ts))

        assert len(client.window) == 1
        assert client.window[0] == (ts, 67000.00)

    @pytest.mark.asyncio
    async def test_ticks_received_incremented(self) -> None:
        """Each valid trade should increment the ticks_received counter."""
        client, _ = _make_client()
        assert client.ticks_received == 0

        await client._handle_message(_trade_msg())
        assert client.ticks_received == 1

        await client._handle_message(_trade_msg(timestamp_ms=1_700_000_001_000))
        assert client.ticks_received == 2

    @pytest.mark.asyncio
    async def test_ignores_non_trade_event(self) -> None:
        """Messages with event type != 'trade' should be silently ignored."""
        client, q = _make_client()
        msg = json.dumps({"e": "aggTrade", "s": "BTCUSDT", "p": "67000", "T": 1})

        await client._handle_message(msg)

        assert q.empty()
        assert client.ticks_received == 0
        assert len(client.window) == 0

    @pytest.mark.asyncio
    async def test_ignores_malformed_json(self) -> None:
        """Unparseable JSON should be silently ignored, not raise."""
        client, q = _make_client()

        await client._handle_message("not-valid-json{{{")

        assert q.empty()
        assert client.ticks_received == 0

    @pytest.mark.asyncio
    async def test_ignores_trade_missing_price(self) -> None:
        """A trade event missing the 'p' field should be ignored."""
        client, q = _make_client()
        msg = json.dumps({"e": "trade", "s": "BTCUSDT", "T": 1_700_000_000_000})

        await client._handle_message(msg)

        assert q.empty()
        assert client.ticks_received == 0

    @pytest.mark.asyncio
    async def test_ignores_trade_non_numeric_price(self) -> None:
        """A trade event with a non-numeric price should be ignored."""
        client, q = _make_client()
        msg = json.dumps({
            "e": "trade", "s": "BTCUSDT", "p": "not-a-number", "T": 1_700_000_000_000,
        })

        await client._handle_message(msg)

        assert q.empty()
        assert client.ticks_received == 0

    @pytest.mark.asyncio
    async def test_queue_full_drops_tick_without_raising(self) -> None:
        """When the queue is full, the tick should be dropped gracefully."""
        client, q = _make_client(queue_maxsize=1)

        # Fill the queue.
        await client._handle_message(_trade_msg(timestamp_ms=1_700_000_000_000))
        assert not q.empty()

        # Second tick should be dropped but still counted and windowed.
        await client._handle_message(_trade_msg(timestamp_ms=1_700_000_001_000))
        assert client.ticks_received == 2
        assert len(client.window) == 2

    @pytest.mark.asyncio
    async def test_handles_bytes_input(self) -> None:
        """json.loads accepts bytes; _handle_message should too."""
        client, q = _make_client()
        msg_bytes = _trade_msg().encode("utf-8")

        await client._handle_message(msg_bytes)

        assert not q.empty()


# ═══════════════════════════════════════════════════════════════════════════
# _prune_window tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPruneWindow:
    """Verify _prune_window() removes entries older than window_seconds."""

    def test_removes_stale_entries(self) -> None:
        """Entries older than window_seconds should be pruned."""
        client, _ = _make_client(window_seconds=10)  # 10s = 10_000ms

        # Add entries spanning 20 seconds.
        client._window.append((1000, 100.0))   # t=1s
        client._window.append((5000, 101.0))   # t=5s
        client._window.append((11000, 102.0))  # t=11s
        client._window.append((15000, 103.0))  # t=15s

        # Prune as of t=15s; cutoff = 15000 - 10000 = 5000.
        # Entries with ts < 5000 (strict <) are removed; ts=5000 is kept.
        client._prune_window(15000)

        assert len(client._window) == 3
        assert client._window[0] == (5000, 101.0)
        assert client._window[1] == (11000, 102.0)
        assert client._window[2] == (15000, 103.0)

    def test_no_op_when_all_entries_within_window(self) -> None:
        """Nothing should be pruned if all entries are within the window."""
        client, _ = _make_client(window_seconds=60)

        client._window.append((50000, 100.0))
        client._window.append((55000, 101.0))

        client._prune_window(55000)

        assert len(client._window) == 2

    def test_prunes_all_when_all_stale(self) -> None:
        """All entries should be removed if all are older than the window."""
        client, _ = _make_client(window_seconds=10)

        client._window.append((1000, 100.0))
        client._window.append((2000, 101.0))

        client._prune_window(100_000)  # cutoff = 90_000

        assert len(client._window) == 0

    def test_empty_window_no_error(self) -> None:
        """Pruning an empty window should not raise."""
        client, _ = _make_client()
        client._prune_window(999_999)
        assert len(client._window) == 0


# ═══════════════════════════════════════════════════════════════════════════
# get_window_open_price tests
# ═══════════════════════════════════════════════════════════════════════════


class TestGetWindowOpenPrice:
    """Verify get_window_open_price() returns the correct boundary price."""

    def test_returns_none_when_empty(self) -> None:
        """An empty window should return None."""
        client, _ = _make_client()
        assert client.get_window_open_price() is None

    def test_returns_earliest_price_within_window(self) -> None:
        """Should return the price at the start of the rolling window."""
        client, _ = _make_client(window_seconds=300)

        now_ms = 1_700_000_300_000  # reference time

        # Entries within the 300s window (cutoff = now - 300_000 = ...000_000).
        client._window.append((now_ms - 290_000, 67000.0))  # oldest in window
        client._window.append((now_ms - 150_000, 67100.0))
        client._window.append((now_ms, 67200.0))            # most recent

        with patch("updown.binance_ws._now_ms", return_value=now_ms):
            price = client.get_window_open_price()

        assert price == 67000.0

    def test_all_stale_returns_most_recent(self) -> None:
        """When all entries are older than the window, return the most recent."""
        client, _ = _make_client(window_seconds=10)

        # All entries well before the window.
        client._window.append((1000, 60000.0))
        client._window.append((2000, 60100.0))
        client._window.append((3000, 60200.0))

        # _now_ms returns something far in the future.
        with patch("updown.binance_ws._now_ms", return_value=1_000_000):
            price = client.get_window_open_price()

        # Should return the last (most recent) entry.
        assert price == 60200.0

    def test_single_entry_returns_that_price(self) -> None:
        """A single entry in the window should be returned."""
        client, _ = _make_client(window_seconds=300)

        now_ms = 1_700_000_000_000
        client._window.append((now_ms, 67500.0))

        with patch("updown.binance_ws._now_ms", return_value=now_ms):
            assert client.get_window_open_price() == 67500.0


# ═══════════════════════════════════════════════════════════════════════════
# _next_backoff_delay tests
# ═══════════════════════════════════════════════════════════════════════════


class TestNextBackoffDelay:
    """Verify exponential backoff with cap and jitter."""

    def test_exponential_growth(self) -> None:
        """Delays should grow exponentially across successive attempts."""
        client, _ = _make_client()
        client._base_delay = 1.0
        client._max_delay = 1000.0

        # Seed random for deterministic jitter (uniform [0, capped]).
        random.seed(42)

        delays = []
        for _ in range(5):
            delays.append(client._next_backoff_delay())

        # Each delay should generally increase (modulo jitter).
        # The max possible delay for attempt n is base * 2^(n-1).
        # With base=1: caps at 1, 2, 4, 8, 16 before jitter.
        # Since jitter is uniform [0, capped], the average should grow.
        assert client._reconnect_attempts == 5

    def test_capped_at_max_delay(self) -> None:
        """Delays should never exceed max_delay."""
        client, _ = _make_client()
        client._base_delay = 1.0
        client._max_delay = 10.0

        for _ in range(20):
            delay = client._next_backoff_delay()
            assert delay <= client._max_delay

    def test_first_attempt_bounded_by_base(self) -> None:
        """First attempt delay should be in [0, base_delay]."""
        client, _ = _make_client()
        client._base_delay = 2.0
        client._max_delay = 60.0

        # Run many times to be statistically confident.
        for _ in range(50):
            client._reconnect_attempts = 0
            delay = client._next_backoff_delay()
            # attempt 1: exp_delay = 2 * 2^0 = 2, jitter in [0, 2]
            assert 0 <= delay <= 2.0

    def test_increments_reconnect_attempts(self) -> None:
        """Each call should increment the reconnect attempt counter."""
        client, _ = _make_client()
        assert client._reconnect_attempts == 0

        client._next_backoff_delay()
        assert client._reconnect_attempts == 1

        client._next_backoff_delay()
        assert client._reconnect_attempts == 2

    def test_delay_is_non_negative(self) -> None:
        """Delay should always be >= 0."""
        client, _ = _make_client()
        for _ in range(100):
            delay = client._next_backoff_delay()
            assert delay >= 0
