"""Async Polymarket CLOB websocket client.

Connects to the Polymarket CLOB websocket, subscribes to market channels,
and maintains a live order-book snapshot (best bid/ask) for each tracked
asset_id.  Emits ``NewMarket`` objects when the server announces a new
market and exposes helper methods to read the latest YES/NO mid-prices.

Reconnection uses exponential backoff with jitter, and all tracked
asset_ids are re-subscribed automatically after a reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

import requests
import websockets
import websockets.exceptions

from config import (
    POLYMARKET_CLOB_REST_URL,
    POLYMARKET_CLOB_WS_URL,
    UPDOWN_HEARTBEAT_INTERVAL_S,
    UPDOWN_RECONNECT_BASE_DELAY_S,
    UPDOWN_RECONNECT_MAX_DELAY_S,
)
from updown.types import NewMarket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal bookkeeping types
# ---------------------------------------------------------------------------

@dataclass
class _BookSide:
    """Best bid or ask for a single side of the book."""
    price: float = 0.0
    size: float = 0.0


@dataclass
class _AssetBook:
    """Tracks best bid/ask for a single asset (YES or NO token)."""
    best_bid: _BookSide = field(default_factory=_BookSide)
    best_ask: _BookSide = field(default_factory=_BookSide)
    last_update_ms: int = 0
    # Server-side midpoint from /midpoint endpoint.  When set, _mid_price
    # uses this instead of computing from best_bid/best_ask (which can be
    # wildly spread on illiquid markets).
    server_mid: Optional[float] = None


class PolymarketWSError(Exception):
    """Raised for unrecoverable Polymarket websocket errors."""


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class PolymarketWSClient:
    """Async Polymarket CLOB websocket client.

    Usage::

        client = PolymarketWSClient()
        client.subscribe("asset_id_123")

        async for event in client.run():
            if isinstance(event, NewMarket):
                print(f"New market: {event.question}")

    The ``run()`` async generator yields ``NewMarket`` objects and never
    returns under normal operation.  On disconnect it automatically
    reconnects with exponential backoff and re-subscribes to all tracked
    asset_ids.
    """

    def __init__(
        self,
        url: str = POLYMARKET_CLOB_WS_URL,
        heartbeat_interval_s: int = UPDOWN_HEARTBEAT_INTERVAL_S,
        reconnect_base_delay_s: float = UPDOWN_RECONNECT_BASE_DELAY_S,
        reconnect_max_delay_s: float = UPDOWN_RECONNECT_MAX_DELAY_S,
        on_new_market: Optional[Callable[[NewMarket], None]] = None,
    ) -> None:
        self._url = url
        self._heartbeat_interval_s = heartbeat_interval_s
        self._reconnect_base_delay_s = reconnect_base_delay_s
        self._reconnect_max_delay_s = reconnect_max_delay_s
        self._on_new_market = on_new_market

        # Tracked asset_ids — survives reconnects.
        self._subscribed_assets: set[str] = set()

        # Live order-book state keyed by asset_id.
        self._books: dict[str, _AssetBook] = {}

        # Internal ws handle, set during run().
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, asset_id: str) -> None:
        """Track an asset_id.  If already connected, sends the subscribe
        message immediately (fire-and-forget via the event loop)."""
        self._subscribed_assets.add(asset_id)
        if asset_id not in self._books:
            self._books[asset_id] = _AssetBook()
        if self._ws is not None:
            asyncio.ensure_future(self._send_subscribe([asset_id]))

    def unsubscribe(self, asset_id: str) -> None:
        """Stop tracking an asset_id."""
        self._subscribed_assets.discard(asset_id)
        self._books.pop(asset_id, None)
        if self._ws is not None:
            asyncio.ensure_future(self._send_unsubscribe([asset_id]))

    def get_yes_price(self, asset_id: str) -> Optional[float]:
        """Return the latest mid-price for a YES token.

        Returns the midpoint of best_bid and best_ask when both are
        available, otherwise falls back to best_bid, then best_ask.
        Returns ``None`` if no data has been received yet.
        """
        return self._mid_price(asset_id)

    def get_no_price(self, asset_id: str) -> Optional[float]:
        """Return the latest mid-price for a NO token.

        For Polymarket binary markets the NO price is ``1 - YES price``.
        Returns ``None`` if no YES data is available.
        """
        yes = self._mid_price(asset_id)
        if yes is None:
            return None
        return round(1.0 - yes, 6)

    def get_book(self, asset_id: str) -> Optional[_AssetBook]:
        """Return the raw book snapshot for an asset, or None."""
        return self._books.get(asset_id)

    @property
    def subscribed_assets(self) -> set[str]:
        return set(self._subscribed_assets)

    def seed_book_from_rest(self, token_id: str) -> None:
        """Fetch the current order book for *token_id* via the CLOB REST API
        and populate ``_books`` so that ``get_yes_price()`` returns a real
        value immediately -- before the WebSocket connection is established.

        This is a **synchronous** call intended to be invoked at startup.
        On any error (network, HTTP, malformed JSON) the method logs a
        warning and returns without crashing so the system can fall back to
        WS-driven prices.
        """
        url = f"{POLYMARKET_CLOB_REST_URL}/book"
        params = {"token_id": token_id}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning(
                "REST seed failed for token %s — will rely on WS updates",
                token_id[:16],
                exc_info=True,
            )
            return

        book = self._books.setdefault(token_id, _AssetBook())
        now_ms = int(time.time() * 1000)

        # Parse bids (descending by price — best bid first).
        bids = data.get("bids", [])
        if bids:
            best = bids[0] if isinstance(bids[0], dict) else {"price": bids[0]}
            book.best_bid = _BookSide(
                price=float(best.get("price", 0)),
                size=float(best.get("size", 0)),
            )

        # Parse asks (ascending by price — best ask first).
        asks = data.get("asks", [])
        if asks:
            best = asks[0] if isinstance(asks[0], dict) else {"price": asks[0]}
            book.best_ask = _BookSide(
                price=float(best.get("price", 0)),
                size=float(best.get("size", 0)),
            )

        book.last_update_ms = now_ms

        # Fetch the server-side midpoint — more accurate than our
        # local (best_bid + best_ask) / 2 for wide-spread markets.
        try:
            mid_resp = requests.get(
                f"{POLYMARKET_CLOB_REST_URL}/midpoint",
                params={"token_id": token_id},
                timeout=10,
            )
            mid_resp.raise_for_status()
            server_mid = float(mid_resp.json().get("mid", 0))
            if server_mid > 0:
                book.server_mid = server_mid
        except Exception:
            logger.debug(
                "Could not fetch /midpoint for %s — using local mid",
                token_id[:16],
            )

        mid = self._mid_price(token_id)
        logger.info(
            "[rest] Seeded book for %s: bid=%.4f ask=%.4f mid=%s (server_mid=%s)",
            token_id[:16],
            book.best_bid.price,
            book.best_ask.price,
            f"{mid:.4f}" if mid is not None else "None",
            f"{book.server_mid:.4f}" if book.server_mid is not None else "None",
        )

    async def stop(self) -> None:
        """Gracefully shut down the client."""
        self._running = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[NewMarket]:
        """Connect, subscribe, and yield NewMarket events forever.

        On any connection drop the method reconnects with exponential
        backoff + jitter and re-subscribes to all tracked assets.
        """
        self._running = True
        consecutive_failures = 0

        while self._running:
            try:
                async with websockets.connect(
                    self._url,
                    additional_headers={"custom_feature_enabled": "true"},
                    ping_interval=None,  # we manage our own pings
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    consecutive_failures = 0
                    logger.info(
                        "Connected to Polymarket CLOB WS at %s", self._url
                    )

                    # Re-subscribe to all tracked assets.
                    if self._subscribed_assets:
                        await self._send_subscribe(list(self._subscribed_assets))

                    # Start the heartbeat/ping task.
                    self._ping_task = asyncio.create_task(
                        self._ping_loop(ws)
                    )

                    try:
                        async for raw_msg in ws:
                            events = self._parse_message(raw_msg)
                            for evt in events:
                                if isinstance(evt, NewMarket):
                                    if self._on_new_market is not None:
                                        self._on_new_market(evt)
                                    yield evt
                    finally:
                        if self._ping_task and not self._ping_task.done():
                            self._ping_task.cancel()
                            try:
                                await self._ping_task
                            except asyncio.CancelledError:
                                pass
                        self._ws = None

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
                asyncio.TimeoutError,
            ) as exc:
                if not self._running:
                    break
                consecutive_failures += 1
                delay = self._backoff_delay(consecutive_failures)
                logger.warning(
                    "Polymarket WS disconnected (%s). Reconnecting in %.1fs "
                    "(attempt %d).",
                    exc,
                    delay,
                    consecutive_failures,
                )
                await asyncio.sleep(delay)

            except Exception:
                if not self._running:
                    break
                consecutive_failures += 1
                delay = self._backoff_delay(consecutive_failures)
                logger.exception(
                    "Unexpected Polymarket WS error. Reconnecting in %.1fs "
                    "(attempt %d).",
                    delay,
                    consecutive_failures,
                )
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Protocol helpers
    # ------------------------------------------------------------------

    async def _send_subscribe(self, asset_ids: list[str]) -> None:
        """Send a subscribe message for the given asset_ids."""
        if self._ws is None:
            return
        msg = json.dumps({
            "type": "subscribe",
            "assets_ids": asset_ids,
            "custom_feature_enabled": True,
        })
        try:
            await self._ws.send(msg)
            logger.debug("Subscribed to assets: %s", asset_ids)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Could not subscribe — connection already closed.")

    async def _send_unsubscribe(self, asset_ids: list[str]) -> None:
        """Send an unsubscribe message for the given asset_ids."""
        if self._ws is None:
            return
        msg = json.dumps({
            "type": "unsubscribe",
            "assets_ids": asset_ids,
        })
        try:
            await self._ws.send(msg)
            logger.debug("Unsubscribed from assets: %s", asset_ids)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send a PING frame every ``_heartbeat_interval_s`` seconds."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_s)
                try:
                    pong = await ws.ping()
                    await asyncio.wait_for(pong, timeout=10)
                    logger.debug("Polymarket WS PING/PONG OK")
                except (
                    websockets.exceptions.ConnectionClosed,
                    asyncio.TimeoutError,
                ):
                    logger.warning("Polymarket WS ping failed — connection lost.")
                    break
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def _parse_message(self, raw: str | bytes) -> list[NewMarket]:
        """Parse a raw websocket message and update internal state.

        Returns a list of ``NewMarket`` objects for any ``new_market``
        events found.  All other event types update the book silently.
        """
        new_markets: list[NewMarket] = []

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Unparseable WS message: %s", raw[:200] if raw else raw)
            return new_markets

        # The CLOB WS may send a single event dict or a list of events.
        events: list[dict] = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("event_type") or event.get("type", "")

            if event_type == "book":
                self._handle_book(event)
            elif event_type == "price_change":
                self._handle_price_change(event)
            elif event_type == "new_market":
                nm = self._handle_new_market(event)
                if nm is not None:
                    new_markets.append(nm)
            elif event_type == "market_resolved":
                self._handle_market_resolved(event)
            # Silently ignore unknown event types (heartbeat acks, etc.)

        return new_markets

    def _handle_book(self, event: dict) -> None:
        """Process a full order-book snapshot.

        Book snapshots often carry wide resting-order spreads (e.g. 0.01/0.99)
        that would clobber tighter best_bid/best_ask values already set by
        ``price_change`` events.  Only update bid/ask when the snapshot spread
        is meaningful (< 50%).  Otherwise, use ``last_trade_price`` as a
        fallback server_mid.
        """
        asset_id = event.get("asset_id", "")
        if not asset_id:
            return

        book = self._books.setdefault(asset_id, _AssetBook())
        now_ms = int(time.time() * 1000)

        # Parse snapshot bid/ask.
        snap_bid = 0.0
        snap_ask = 0.0
        bids = event.get("bids", [])
        if bids:
            best = bids[0] if isinstance(bids[0], dict) else {"price": bids[0]}
            snap_bid = float(best.get("price", 0))

        asks = event.get("asks", [])
        if asks:
            best = asks[0] if isinstance(asks[0], dict) else {"price": asks[0]}
            snap_ask = float(best.get("price", 0))

        spread = snap_ask - snap_bid if snap_bid > 0 and snap_ask > 0 else 1.0

        if spread < 0.50:
            # Tight spread — use the snapshot bid/ask directly.
            book.best_bid = _BookSide(price=snap_bid, size=float(bids[0].get("size", 0)) if bids else 0)
            book.best_ask = _BookSide(price=snap_ask, size=float(asks[0].get("size", 0)) if asks else 0)
            book.server_mid = None
        else:
            # Wide spread — don't clobber existing bid/ask from price_change
            # events.  Use last_trade_price as server_mid fallback.
            ltp = event.get("last_trade_price")
            if ltp is not None:
                book.server_mid = float(ltp)

        book.last_update_ms = now_ms

    def _handle_price_change(self, event: dict) -> None:
        """Process a price_change event containing a ``price_changes`` array.

        Each item in the array carries ``asset_id``, ``best_bid``, and
        ``best_ask`` — the actual top-of-book after the change.  We use
        those directly instead of the per-order ``price``/``side`` fields,
        which refer to individual resting orders (often at the extremes).
        """
        now_ms = int(time.time() * 1000)

        changes = event.get("price_changes", [])
        for change in changes:
            asset_id = change.get("asset_id", "")
            if not asset_id or asset_id not in self._subscribed_assets:
                continue

            book = self._books.setdefault(asset_id, _AssetBook())

            best_bid = change.get("best_bid")
            best_ask = change.get("best_ask")

            if best_bid is not None:
                book.best_bid = _BookSide(price=float(best_bid), size=0)
            if best_ask is not None:
                book.best_ask = _BookSide(price=float(best_ask), size=0)

            # Real best_bid/best_ask from WS supersedes the REST seed.
            book.server_mid = None
            book.last_update_ms = now_ms

    def _handle_new_market(self, event: dict) -> Optional[NewMarket]:
        """Process a new-market announcement and return a typed object."""
        market_id = event.get("market_id") or event.get("condition_id", "")
        question = event.get("question", "")
        token_id = event.get("token_id") or event.get("asset_id", "")

        if not market_id:
            logger.debug("new_market event missing market_id: %s", event)
            return None

        yes_price = float(event.get("yes_price", 0.5))
        no_price = float(event.get("no_price", 1.0 - yes_price))
        tags = event.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        nm = NewMarket(
            market_id=market_id,
            question=question,
            token_id=token_id,
            yes_price=yes_price,
            no_price=no_price,
            tags=tags,
            discovered_at=event.get("timestamp", ""),
        )
        logger.info("New market discovered: %s — %s", market_id, question)
        return nm

    def _handle_market_resolved(self, event: dict) -> None:
        """Process a market-resolved event — remove from tracked books."""
        asset_id = event.get("asset_id", "")
        market_id = event.get("market_id", "")
        logger.info(
            "Market resolved: market_id=%s asset_id=%s outcome=%s",
            market_id,
            asset_id,
            event.get("outcome", "unknown"),
        )
        # Clean up tracked state if this asset was subscribed.
        if asset_id:
            self._subscribed_assets.discard(asset_id)
            self._books.pop(asset_id, None)

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _mid_price(self, asset_id: str) -> Optional[float]:
        """Return the best available price for an asset.

        Preference order:
        1. Server-side midpoint (from /midpoint REST endpoint) — most
           accurate for wide-spread markets.
        2. Local midpoint when both bid and ask are available.
        3. Whichever side has data.
        4. ``None`` when no data exists.
        """
        book = self._books.get(asset_id)
        if book is None:
            return None

        if book.server_mid is not None:
            return book.server_mid

        bid = book.best_bid.price
        ask = book.best_ask.price

        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 6)
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return None

    # ------------------------------------------------------------------
    # Backoff
    # ------------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """Compute exponential backoff delay with jitter.

        ``delay = min(base * 2^(attempt-1) + jitter, max_delay)``
        """
        exp_delay = self._reconnect_base_delay_s * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._reconnect_base_delay_s)
        return min(exp_delay + jitter, self._reconnect_max_delay_s)
