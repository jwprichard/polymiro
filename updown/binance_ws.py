"""Async Binance WebSocket client for real-time BTC/USDT trade updates.

Connects to the Binance trade stream, publishes PriceUpdate events to an
asyncio.Queue, and maintains a rolling window of recent prices for the
signal engine to query.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import random
import time
from collections import deque
from typing import Optional

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidURI,
    WebSocketException,
)

from config import (
    BINANCE_WS_URL,
    UPDOWN_RECONNECT_BASE_DELAY_S,
    UPDOWN_RECONNECT_MAX_DELAY_S,
    UPDOWN_WINDOW_SECONDS,
)
from updown.types import PriceUpdate

logger = logging.getLogger(__name__)


class BinanceWSError(Exception):
    """Raised for non-recoverable Binance WebSocket errors."""


class BinanceWS:
    """Async Binance BTC/USDT trade stream client.

    Parameters
    ----------
    queue : asyncio.Queue[PriceUpdate]
        Queue that receives every parsed trade tick.  Shared with the
        signal engine so it can react to price changes in real time.
    window_seconds : int
        Length of the rolling price window in seconds.  Defaults to the
        ``UPDOWN_WINDOW_SECONDS`` config value (300 s / 5 min).
    ws_url : str
        Binance WebSocket endpoint.  Defaults to ``BINANCE_WS_URL``.
    """

    def __init__(
        self,
        queue: asyncio.Queue[PriceUpdate],
        *,
        window_seconds: int = UPDOWN_WINDOW_SECONDS,
        ws_url: str = BINANCE_WS_URL,
    ) -> None:
        self._queue = queue
        self._ws_url = ws_url
        self._window_seconds = window_seconds

        # Rolling window: deque of (timestamp_ms, price) sorted by time.
        self._window: deque[tuple[int, float]] = deque()

        # Reconnection back-off state.
        self._reconnect_attempts: int = 0
        self._base_delay: float = UPDOWN_RECONNECT_BASE_DELAY_S
        self._max_delay: float = UPDOWN_RECONNECT_MAX_DELAY_S

        # Bookkeeping.
        self._running: bool = False
        self._ticks_received: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect and stream trades forever (until cancelled).

        Reconnects automatically with exponential back-off + jitter on any
        disconnect or transient error.  Raises ``asyncio.CancelledError``
        cleanly when the caller cancels the task.
        """
        self._running = True
        logger.info("BinanceWS starting — target %s", self._ws_url)

        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                logger.info("BinanceWS cancelled — shutting down cleanly")
                self._running = False
                raise
            except (ConnectionClosed, ConnectionClosedError, WebSocketException, OSError) as exc:
                delay = self._next_backoff_delay()
                logger.warning(
                    "BinanceWS connection lost (%s) — reconnecting in %.1fs (attempt %d)",
                    exc,
                    delay,
                    self._reconnect_attempts,
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                delay = self._next_backoff_delay()
                logger.error(
                    "BinanceWS unexpected error (%s: %s) — reconnecting in %.1fs (attempt %d)",
                    type(exc).__name__,
                    exc,
                    delay,
                    self._reconnect_attempts,
                )
                await asyncio.sleep(delay)

    def get_window_open_price(self) -> Optional[float]:
        """Return the price at the start of the rolling window.

        Uses ``bisect`` on the sorted timestamp deque to find the earliest
        tick at or after ``(now - window_seconds)``.  Returns ``None`` if
        the window has no data yet.
        """
        if not self._window:
            return None

        cutoff_ms = _now_ms() - self._window_seconds * 1000
        # Find the leftmost entry whose timestamp >= cutoff_ms.
        idx = bisect.bisect_left(self._window, (cutoff_ms,))

        if idx < len(self._window):
            return self._window[idx][1]

        # All entries are older than the cutoff — return the most recent.
        return self._window[-1][1]

    @property
    def window(self) -> deque[tuple[int, float]]:
        """Read-only access to the rolling price window."""
        return self._window

    @property
    def ticks_received(self) -> int:
        """Total number of trade ticks processed since startup."""
        return self._ticks_received

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_and_stream(self) -> None:
        """Open a single WebSocket connection and process messages until
        the connection drops or an error occurs."""
        logger.info("BinanceWS connecting to %s", self._ws_url)

        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("BinanceWS connected")
            self._reconnect_attempts = 0  # Reset on successful connect.

            async for raw_msg in ws:
                await self._handle_message(raw_msg)

        # If we exit the async for normally the server closed the connection.
        logger.info("BinanceWS server closed the connection")

    async def _handle_message(self, raw_msg: str | bytes) -> None:
        """Parse a single Binance trade message and propagate it."""
        try:
            data = json.loads(raw_msg)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("BinanceWS ignoring unparseable message: %s", exc)
            return

        # Binance trade stream payload fields:
        #   e: event type ("trade")
        #   s: symbol ("BTCUSDT")
        #   p: price (string)
        #   T: trade time (epoch ms)
        if data.get("e") != "trade":
            return

        try:
            price = float(data["p"])
            timestamp_ms = int(data["T"])
            symbol = str(data["s"])
        except (KeyError, ValueError) as exc:
            logger.debug("BinanceWS malformed trade payload: %s", exc)
            return

        update = PriceUpdate(symbol=symbol, price=price, timestamp_ms=timestamp_ms)

        # Append to the rolling window and prune stale entries.
        self._window.append((timestamp_ms, price))
        self._prune_window(timestamp_ms)

        # Publish to the shared queue (non-blocking; drops if full to
        # avoid back-pressure stalling the WS read loop).
        try:
            self._queue.put_nowait(update)
        except asyncio.QueueFull:
            logger.debug("BinanceWS queue full — dropping tick at %d", timestamp_ms)

        self._ticks_received += 1

    def _prune_window(self, latest_ms: int) -> None:
        """Remove entries older than ``window_seconds`` from the left."""
        cutoff_ms = latest_ms - self._window_seconds * 1000
        while self._window and self._window[0][0] < cutoff_ms:
            self._window.popleft()

    def _next_backoff_delay(self) -> float:
        """Compute the next reconnect delay with exponential back-off and jitter."""
        self._reconnect_attempts += 1
        exp_delay = self._base_delay * (2 ** (self._reconnect_attempts - 1))
        capped = min(exp_delay, self._max_delay)
        # Full jitter: uniform random in [0, capped].
        return random.uniform(0, capped)


def _now_ms() -> int:
    """Current wall-clock time in epoch milliseconds."""
    return int(time.time() * 1000)
