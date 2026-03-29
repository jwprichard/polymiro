"""Updown orchestrator loop -- coordinates all real-time components.

Launches the Binance WS price stream and Polymarket CLOB WS client as
concurrent asyncio tasks, reacts to price ticks by computing momentum
signals, and executes trades when edge exceeds the configured threshold.

Usage::

    import asyncio
    from updown.loop import run

    asyncio.run(run())
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from updown.binance_ws import BinanceWS
from updown.executor import place_order
from updown.polymarket_ws import PolymarketWSClient
from updown.signal import compute_signal
from updown.types import (
    MarketSnapshot,
    OrderResult,
    PriceUpdate,
    SignalResult,
    TradeIntent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup market seeding
# ---------------------------------------------------------------------------


def _seed_markets_from_rest() -> list[tuple[str, str, str, float]]:
    """Fetch the current BTC 5-min up/down market from the Gamma API.

    Computes the slug deterministically from the current time:
        btc-updown-5m-{floor(now / 300) * 300}

    Returns a list with at most one (condition_id, question, token_id,
    window_end_epoch) tuple.  The window_end is start + 300s.

    Runs synchronously (called once at startup before the async loop).
    """
    import json as _json

    import requests

    now = time.time()
    window_start = int(now // 300) * 300
    window_end = window_start + 300
    slug = f"btc-updown-5m-{window_start}"

    logger.info("[gamma] Looking up current market by slug: %s", slug)

    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("[gamma] Failed to fetch market for slug %s", slug)
        return []

    # Gamma returns a list when querying by slug
    records = data if isinstance(data, list) else [data]
    if not records or not isinstance(records[0], dict):
        logger.warning("[gamma] No market found for slug %s", slug)
        return []

    record = records[0]
    question = record.get("question", "")
    condition_id = record.get("conditionId") or record.get("condition_id", "")

    raw_token_ids = record.get("clobTokenIds", [])
    if isinstance(raw_token_ids, str):
        try:
            raw_token_ids = _json.loads(raw_token_ids)
        except (ValueError, _json.JSONDecodeError):
            raw_token_ids = []
    token_id = raw_token_ids[0] if raw_token_ids else ""

    if not condition_id or not token_id:
        logger.warning("[gamma] Market %s missing conditionId or tokenId", slug)
        return []

    logger.info("[gamma] Found market: %s (ends %.0fs from now)", question, window_end - now)
    return [(condition_id, question, token_id, float(window_end))]


def _seed_prices_from_rest(
    polymarket: PolymarketWSClient,
    tracked_markets: dict[str, TrackedMarket],
) -> None:
    """Fetch initial order-book prices from the CLOB REST API for every
    seeded token so ``get_yes_price()`` returns real values immediately,
    before the WebSocket connection is established.

    Errors for individual tokens are handled inside
    ``seed_book_from_rest`` (log + continue), so this never crashes.
    """
    token_count = 0
    for tracked in tracked_markets.values():
        for token_id in tracked.asset_ids:
            polymarket.seed_book_from_rest(token_id)
            token_count += 1

    logger.info(
        "[rest] Bootstrapped prices for %d tokens across %d markets",
        token_count,
        len(tracked_markets),
    )




# ---------------------------------------------------------------------------
# Tracked market state
# ---------------------------------------------------------------------------

@dataclass
class TrackedMarket:
    """State for a single tracked Polymarket up/down market."""

    condition_id: str
    question: str
    asset_ids: list[str] = field(default_factory=list)
    expiry_time: float = 0.0  # epoch seconds
    traded: bool = False
    last_trade_time: float = 0.0  # epoch seconds -- cooldown tracking
    discovered_at: float = field(default_factory=time.time)


# Per-market cooldown: one trade per 5-minute window.
_COOLDOWN_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run() -> None:
    """Main entry point -- start all components and coordinate them.

    This coroutine runs until cancelled or a SIGINT/SIGTERM is received.
    It launches:

    1. BinanceWS -- streams BTC/USDT trades into a shared asyncio.Queue.
    2. PolymarketWSClient -- streams CLOB events; updates book prices.
    3. A tick-processing loop that reads from the Binance queue, computes
       signals for every tracked market, and executes trades when edge
       exceeds the threshold.

    All three are run as concurrent asyncio.Tasks and are cancelled
    together on shutdown.
    """

    # --- Startup banner ---------------------------------------------------
    logger.info("=" * 60)
    logger.info("UPDOWN ORCHESTRATOR STARTING")
    logger.info("-" * 60)
    logger.info("  dry_mode        : %s", config.UPDOWN_DRY_MODE)
    logger.info("  scale_factor    : %.6f", config.UPDOWN_SCALE_FACTOR)
    logger.info("  min_btc_pct_chg : %.4f%% (%.6f)", config.UPDOWN_MIN_BTC_PCT_CHANGE * 100, config.UPDOWN_MIN_BTC_PCT_CHANGE)
    logger.info("  edge_threshold  : %.4f", config.UPDOWN_EDGE_THRESHOLD)
    logger.info("  trade_amount    : %.2f USDC", config.UPDOWN_TRADE_AMOUNT_USDC)
    logger.info("  window_seconds  : %d s", config.UPDOWN_WINDOW_SECONDS)
    logger.info("  binance_ws      : %s", config.BINANCE_WS_URL)
    logger.info("  polymarket_ws   : %s", config.POLYMARKET_CLOB_WS_URL)
    logger.info("  polymarket_rest : %s", config.POLYMARKET_CLOB_REST_URL)
    logger.info("=" * 60)

    # --- Shared state -----------------------------------------------------
    price_queue: asyncio.Queue[PriceUpdate] = asyncio.Queue(maxsize=4096)
    tracked_markets: dict[str, TrackedMarket] = {}

    # --- Component instances ----------------------------------------------
    binance = BinanceWS(price_queue)
    polymarket = PolymarketWSClient()

    # --- Seed the current BTC 5-min market by slug -------------------------
    seeded = _seed_markets_from_rest()
    for condition_id, question, token_id, window_end in seeded:
        if condition_id not in tracked_markets:
            tracked = TrackedMarket(
                condition_id=condition_id,
                question=question,
                asset_ids=[token_id],
                expiry_time=window_end,
                traded=False,
            )
            tracked_markets[condition_id] = tracked
            polymarket.subscribe(token_id)
            logger.info(
                "[gamma] Seeded market: %s — %s (token=%s)",
                condition_id[:16],
                question[:80],
                token_id[:12],
            )

    # --- Bootstrap real prices from CLOB REST before WS connects ----------
    _seed_prices_from_rest(polymarket, tracked_markets)

    # --- Shutdown plumbing ------------------------------------------------
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal -- initiating graceful shutdown")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # add_signal_handler is not available on Windows.
            pass

    # --- Task definitions -------------------------------------------------

    async def _binance_task() -> None:
        """Run the Binance WS client until cancelled."""
        await binance.run()

    async def _polymarket_task() -> None:
        """Run the Polymarket WS client for live price updates."""
        await polymarket.run()

    async def _tick_processor() -> None:
        """Consume Binance price ticks and evaluate signals for tracked markets."""
        tick_count = 0
        last_heartbeat = time.time()
        while not shutdown_event.is_set():
            try:
                # Wait for the next price tick with a timeout so we can
                # check the shutdown event periodically.
                try:
                    tick = await asyncio.wait_for(price_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                tick_count += 1
                now = time.time()
                if now - last_heartbeat >= config.UPDOWN_HEARTBEAT_INTERVAL_S:
                    open_price = binance.get_window_open_price()
                    pct_chg = ""
                    if open_price and open_price > 0:
                        pct_chg = f" chg={((tick.price - open_price) / open_price) * 100:+.3f}%"
                    logger.info(
                        "[binance] Heartbeat: %d ticks | %d markets | BTC=%.2f%s",
                        tick_count,
                        len(tracked_markets),
                        tick.price,
                        pct_chg,
                    )
                    for cid, tm in tracked_markets.items():
                        for aid in tm.asset_ids:
                            yp = polymarket.get_yes_price(aid)
                            np_ = polymarket.get_no_price(aid)
                            ttl = tm.expiry_time - now
                            logger.info(
                                "[poly]  %s | YES=%.3f NO=%.3f | traded=%s TTL=%.0fs",
                                tm.question[26:50],
                                yp or 0,
                                np_ or 0,
                                tm.traded,
                                max(ttl, 0),
                            )
                    last_heartbeat = now

                await _process_tick(tick, binance, polymarket, tracked_markets)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Tick processor encountered an error")

    # --- Launch tasks -----------------------------------------------------
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_binance_task(), name="binance_ws"),
        asyncio.create_task(_polymarket_task(), name="polymarket_ws"),
        asyncio.create_task(_tick_processor(), name="tick_processor"),
    ]

    try:
        # Wait until shutdown is requested or any task crashes.
        done, _pending = await asyncio.wait(
            [asyncio.create_task(shutdown_event.wait(), name="shutdown_waiter"), *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If a task finished before the shutdown signal, log the reason.
        for task in done:
            if task.get_name() != "shutdown_waiter" and task.exception():
                logger.error(
                    "Task %s crashed: %s", task.get_name(), task.exception()
                )

    finally:
        # --- Graceful shutdown --------------------------------------------
        logger.info("Shutting down updown orchestrator...")

        # Cancel all component tasks.
        for task in tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to finish cancellation.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Close websocket connections.
        await polymarket.stop()

        # Log final state.
        logger.info("Final state: %d tracked markets", len(tracked_markets))
        for cid, tm in tracked_markets.items():
            logger.info(
                "  %s traded=%s question=%s",
                cid[:16],
                tm.traded,
                tm.question[:60],
            )
        logger.info("Updown orchestrator stopped.")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------



def _handle_market_resolved(
    condition_id: str,
    tracked_markets: dict[str, TrackedMarket],
    polymarket: PolymarketWSClient,
) -> None:
    """Remove a resolved market from tracking and unsubscribe from its assets."""

    tracked = tracked_markets.pop(condition_id, None)
    if tracked is None:
        return

    for aid in tracked.asset_ids:
        polymarket.unsubscribe(aid)

    logger.info(
        "[poly] Market resolved and removed: %s -- %s (traded=%s)",
        condition_id[:16],
        tracked.question[:60],
        tracked.traded,
    )


# ---------------------------------------------------------------------------
# Tick processing
# ---------------------------------------------------------------------------

async def _process_tick(
    tick: PriceUpdate,
    binance: BinanceWS,
    polymarket: PolymarketWSClient,
    tracked_markets: dict[str, TrackedMarket],
) -> None:
    """Evaluate every tracked market against the latest BTC price tick."""

    open_price = binance.get_window_open_price()
    if open_price is None:
        # Not enough data in the window yet -- skip.
        return

    current_price = tick.price
    now = time.time()

    # Prune expired markets.
    expired = [
        cid for cid, tm in tracked_markets.items()
        if tm.expiry_time > 0 and now > tm.expiry_time
    ]
    for cid in expired:
        _handle_market_resolved(cid, tracked_markets, polymarket)

    # Evaluate each tracked market.
    for condition_id, tracked in list(tracked_markets.items()):
        # Cooldown check: at most one trade per 5-min window per market.
        if tracked.traded and (now - tracked.last_trade_time) < _COOLDOWN_SECONDS:
            continue

        for asset_id in tracked.asset_ids:
            yes_price = polymarket.get_yes_price(asset_id)
            if yes_price is None:
                continue

            # Compute signal.
            no_price = polymarket.get_no_price(asset_id)
            try:
                sig = compute_signal(
                    current_price=current_price,
                    open_price=open_price,
                    market_yes_price=yes_price,
                    threshold=config.UPDOWN_EDGE_THRESHOLD,
                )
            except ValueError as exc:
                logger.warning("Signal computation error: %s", exc)
                continue

            pct_change = (current_price - open_price) / open_price * 100
            ttl = tracked.expiry_time - now
            logger.debug(
                "[poly/binance] %s | YES=%.3f NO=%.3f spread=%.3f | "
                "BTC=%.2f open=%.2f chg=%+.3f%% | "
                "implied=%.3f edge=%+.4f dir=%s trade=%s | TTL=%.0fs",
                tracked.question[26:50],  # time window portion
                yes_price,
                no_price if no_price is not None else 1.0 - yes_price,
                abs(yes_price - (no_price or (1.0 - yes_price))),
                current_price,
                open_price,
                pct_change,
                sig.implied_probability,
                sig.edge,
                sig.direction,
                sig.should_trade,
                max(ttl, 0),
            )

            if not sig.should_trade:
                continue

            # Build trade intent.
            snapshot = MarketSnapshot(
                market_id=condition_id,
                question=tracked.question,
                token_id=asset_id,
                yes_price=yes_price,
                no_price=no_price if no_price is not None else 1.0 - yes_price,
                spread=abs(yes_price - (no_price or (1.0 - yes_price))),
                timestamp_ms=tick.timestamp_ms,
            )

            intent = TradeIntent(
                market_id=condition_id,
                token_id=asset_id,
                side="buy",
                outcome=sig.direction.lower(),  # "yes" or "no"
                size_usdc=config.UPDOWN_TRADE_AMOUNT_USDC,
                signal=sig,
                market=snapshot,
                reason=(
                    f"BTC {sig.direction} momentum | edge={sig.edge:+.4f} "
                    f"implied={sig.implied_probability:.4f} "
                    f"market={sig.market_price:.4f}"
                ),
            )

            logger.info(
                "[signal] Triggered for %s: direction=%s edge=%.4f implied=%.4f market=%.4f",
                condition_id[:16],
                sig.direction,
                sig.edge,
                sig.implied_probability,
                sig.market_price,
            )

            # Execute trade.
            try:
                result: OrderResult = await place_order(
                    intent,
                    edge=sig.edge,
                    implied_prob=sig.implied_probability,
                    market_price=sig.market_price,
                )

                if result.success:
                    tracked.traded = True
                    tracked.last_trade_time = now
                    logger.info(
                        "[poly] Trade executed for %s: order_id=%s filled_price=%.4f",
                        condition_id[:16],
                        result.order_id,
                        result.filled_price or 0.0,
                    )
                else:
                    logger.warning(
                        "[poly] Trade failed for %s: %s",
                        condition_id[:16],
                        result.error,
                    )

            except Exception:
                logger.exception(
                    "Unexpected error executing trade for %s", condition_id[:16]
                )
