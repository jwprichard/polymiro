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
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from common import config
from common.log import ulog
from updown.binance_ws import BinanceWS
from updown import executor as _executor
from updown.decisions import evaluate_entry, evaluate_exit, evaluate_expiry
from updown.executor import build_exit_intent, drain_latency_stats, place_order
from updown.exit_rules import ExitSignal
from updown.polymarket_ws import PolymarketWSClient
from updown.strategy_config import StrategyConfig
from updown.retry import retry_async
from updown.tick_log import TickLogger, TradeEventLogger
from updown.types import (
    MarketState,
    OrderResult,
    PriceUpdate,
    TickContext,
    TradeIntent,
    get_exchange_now_ms,
    transition,
)

# Module-level loggers — instantiated once, reused across all ticks/events.
_tick_logger = TickLogger()
_event_logger = TradeEventLogger()

# ---------------------------------------------------------------------------
# Queue backpressure
# ---------------------------------------------------------------------------

# When the price queue exceeds this depth, drain to the most recent tick so
# signals always reflect current BTC price rather than stale history.
_QUEUE_DRAIN_THRESHOLD: int = 50

# Module-level counter for dashboard / operational visibility.
ticks_drained: int = 0


def drain_to_latest(
    queue: asyncio.Queue[PriceUpdate],
    current_tick: PriceUpdate,
) -> tuple[PriceUpdate, int]:
    """Drain *queue* and return the most recent tick along with the drain count.

    Consumes all items currently in *queue* via ``get_nowait()``, keeping
    only the last one.  If the queue turns out to be empty during the drain
    (race between ``qsize()`` check and actual reads), *current_tick* is
    returned unchanged with a drain count of 0.

    Returns
    -------
    (latest_tick, drained_count)
        *latest_tick* is guaranteed to never be ``None``.
    """
    drained = 0
    latest = current_tick
    while True:
        try:
            latest = queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    return latest, drained


# ---------------------------------------------------------------------------
# Startup market seeding
# ---------------------------------------------------------------------------


async def _seed_markets_from_rest(
    session: aiohttp.ClientSession,
) -> list[tuple[str, str, str, float]]:
    """Fetch the current BTC 5-min up/down market from the Gamma API.

    Computes the slug deterministically from the current time:
        btc-updown-5m-{floor(now / 300) * 300}

    Returns a list with at most one (condition_id, question, token_id,
    window_end_epoch) tuple.  The window_end is start + 300s.

    Uses the shared *session* for non-blocking HTTP requests.
    """
    import json as _json

    now = time.time()
    window_start = int(now // 300) * 300
    window_end = window_start + 300
    slug = f"btc-updown-5m-{window_start}"

    ulog.gamma.info("Looking up current market by slug: %s", slug)

    timeout = aiohttp.ClientTimeout(total=15)

    async def _fetch() -> dict | list:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    try:
        data = await retry_async(
            _fetch,
            description=f"gamma market fetch for {slug}",
        )
    except Exception:
        ulog.gamma.error("All retries exhausted for slug %s — continuing without seed", slug)
        return []

    # Gamma returns a list when querying by slug
    records = data if isinstance(data, list) else [data]
    if not records or not isinstance(records[0], dict):
        ulog.gamma.warning("No market found for slug %s", slug)
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
        ulog.gamma.warning("Market %s missing conditionId or tokenId", slug)
        return []

    ulog.gamma.info("Found market: %s (ends %.0fs from now)", question, window_end - now)
    return [(condition_id, question, token_id, float(window_end))]


async def _seed_prices_from_rest(
    polymarket: PolymarketWSClient,
    tracked_markets: dict[str, TrackedMarket],
    session: aiohttp.ClientSession,
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
            await polymarket.seed_book_from_rest(token_id, session)
            token_count += 1

    ulog.rest.info(
        "Bootstrapped prices for %d tokens across %d markets",
        token_count,
        len(tracked_markets),
    )


async def _seed_next_window_market(
    session: aiohttp.ClientSession,
    expiring_window_end: float,
) -> list[tuple[str, str, str, float]]:
    """Fetch the *next* BTC 5-min market (the one starting when the
    current window expires).

    Unlike ``_seed_markets_from_rest`` which derives the slug from
    ``time.time()``, this function computes the slug from the known
    *expiring_window_end* epoch so it always targets the upcoming window
    even when called before the current window has actually expired.

    Returns a list with at most one (condition_id, question, token_id,
    window_end_epoch) tuple.
    """
    import json as _json

    next_window_start = int(expiring_window_end)
    next_window_end = next_window_start + 300
    slug = f"btc-updown-5m-{next_window_start}"

    ulog.gamma.info("Looking up next-window market by slug: %s", slug)

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception:
        ulog.gamma.exception("Failed to fetch next-window market for slug %s", slug)
        return []

    records = data if isinstance(data, list) else [data]
    if not records or not isinstance(records[0], dict):
        ulog.gamma.warning("No market found for next-window slug %s", slug)
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
        ulog.gamma.warning("Next-window market %s missing conditionId or tokenId", slug)
        return []

    ulog.gamma.info(
        "Found next-window market: %s (starts in %.0fs)",
        question,
        next_window_start - time.time(),
    )
    return [(condition_id, question, token_id, float(next_window_end))]



# ---------------------------------------------------------------------------
# Tracked market state
# ---------------------------------------------------------------------------

@dataclass
class TrackedMarket:
    """State for a single tracked Polymarket up/down market.

    Lifecycle is managed via the ``state`` field (a ``MarketState`` enum)
    instead of the former ``traded`` / ``pending_order`` / ``last_trade_time``
    booleans.  Use ``transition()`` from ``updown.types`` for state changes
    so that invalid transitions are caught immediately.
    """

    condition_id: str
    question: str
    asset_ids: list[str] = field(default_factory=list)
    expiry_time: float = 0.0  # epoch seconds
    state: MarketState = MarketState.IDLE
    cooldown_until: float = 0.0  # epoch seconds -- active when state == COOLDOWN
    discovered_at: float = 0.0  # epoch seconds -- caller should supply explicitly

    # Position tracking -- populated after a successful buy.
    entry_price: Optional[float] = None
    entry_time: Optional[float] = None  # epoch seconds
    entry_side: Optional[str] = None  # "yes" or "no"
    entry_size_usdc: Optional[float] = None

    @property
    def has_open_position(self) -> bool:
        """True when the market is in the ENTERED state."""
        return self.state == MarketState.ENTERED


# Per-market cooldown: one trade per 5-minute window.
_COOLDOWN_SECONDS: float = 300.0

# Maximum age (in ms) of a Polymarket price before it is considered stale.
# Stale prices are rejected before signal computation or exit rule evaluation.
_MAX_PRICE_AGE_MS: int = 1000

# When the most recent WS update is older than this, the heartbeat re-seeds
# the book from REST to recover from silent WS failures.
_REST_RESEED_AGE_MS: int = 10_000


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run(strategy_config: StrategyConfig | None = None) -> None:
    """Main entry point -- start all components and coordinate them.

    Parameters
    ----------
    strategy_config:
        Typed strategy configuration loaded from strategy YAML.  When
        provided, exit rules are evaluated on every tick for markets
        with open positions.

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
    ulog.startup.info("=" * 60)
    ulog.startup.info("UPDOWN ORCHESTRATOR STARTING")
    ulog.startup.info("-" * 60)
    ulog.startup.info("  dry_mode        : %s", config.UPDOWN_DRY_MODE)
    ulog.startup.info("  scale_factor    : %.6f", config.UPDOWN_SCALE_FACTOR)
    ulog.startup.info("  min_btc_pct_chg : %.4f%% (%.6f)", config.UPDOWN_MIN_BTC_PCT_CHANGE * 100, config.UPDOWN_MIN_BTC_PCT_CHANGE)
    ulog.startup.info("  edge_threshold  : %.4f", config.UPDOWN_EDGE_THRESHOLD)
    ulog.startup.info("  trade_amount    : %.2f USDC", config.UPDOWN_TRADE_AMOUNT_USDC)
    ulog.startup.info("  window_seconds  : %d s", config.UPDOWN_WINDOW_SECONDS)
    ulog.startup.info("  rotation_lead   : %.1f s", config.UPDOWN_ROTATION_LEAD_TIME_S)
    ulog.startup.info("  binance_ws      : %s", config.BINANCE_WS_URL)
    ulog.startup.info("  polymarket_ws   : %s", config.POLYMARKET_CLOB_WS_URL)
    ulog.startup.info("  polymarket_rest : %s", config.POLYMARKET_CLOB_REST_URL)
    if strategy_config is not None:
        er = strategy_config.exit_rules
        ulog.startup.info("  exit_rules      : tp=%s(%.4f) sl=%s(%.4f) te=%s(%.1fs) reentry=%s",
                     er.take_profit.enabled, er.take_profit.target_delta,
                     er.stop_loss.enabled, er.stop_loss.max_loss_delta,
                     er.time_exit.enabled, er.time_exit.max_hold_seconds,
                     er.allow_reentry)
    else:
        ulog.startup.info("  exit_rules      : DISABLED (no strategy config)")
    ulog.startup.info("=" * 60)

    # --- Shared state -----------------------------------------------------
    price_queue: asyncio.Queue[PriceUpdate] = asyncio.Queue(maxsize=4096)
    tracked_markets: dict[str, TrackedMarket] = {}

    # --- Component instances ----------------------------------------------
    binance = BinanceWS(price_queue)
    polymarket = PolymarketWSClient()

    # --- Shared aiohttp session for all REST calls ------------------------
    http_session = aiohttp.ClientSession()

    # --- Seed the current BTC 5-min market by slug -------------------------
    seeded = await _seed_markets_from_rest(http_session)
    for condition_id, question, token_id, window_end in seeded:
        if condition_id not in tracked_markets:
            tracked = TrackedMarket(
                condition_id=condition_id,
                question=question,
                asset_ids=[token_id],
                expiry_time=window_end,
                discovered_at=time.time(),
            )
            tracked_markets[condition_id] = tracked
            polymarket.subscribe(token_id)
            ulog.gamma.info(
                "Seeded market: %s — %s (token=%s)",
                condition_id[:16],
                question[:80],
                token_id[:12],
            )

    # --- Bootstrap real prices from CLOB REST before WS connects ----------
    await _seed_prices_from_rest(polymarket, tracked_markets, http_session)

    # --- Shutdown plumbing ------------------------------------------------
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        ulog.startup.info("Received shutdown signal -- initiating graceful shutdown")
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
        last_poly_log = 0.0
        while not shutdown_event.is_set():
            try:
                # Wait for the next price tick with a timeout so we can
                # check the shutdown event periodically.
                try:
                    tick = await asyncio.wait_for(price_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # --- Backpressure: drain stale ticks if queue backed up ---
                global ticks_drained
                qsize = price_queue.qsize()
                if qsize > _QUEUE_DRAIN_THRESHOLD:
                    tick, drained = drain_to_latest(price_queue, tick)
                    if drained > 0:
                        ticks_drained += drained
                        ulog.backpressure.warning(
                            "Drained %d stale ticks (queue was %d, total drained=%d)",
                            drained,
                            qsize,
                            ticks_drained,
                        )

                tick_count += 1
                now = time.time()
                if now - last_heartbeat >= config.UPDOWN_HEARTBEAT_INTERVAL_S:
                    open_price = binance.get_window_open_price()
                    pct_chg = ""
                    if open_price and open_price > 0:
                        pct_chg = f" chg={((tick.price - open_price) / open_price) * 100:+.3f}%"
                    avg_lat, max_lat, lat_orders = drain_latency_stats()
                    ulog.binance.info(
                        "Heartbeat: %d ticks | %d markets | BTC=%.2f%s | qsize=%d drained=%d slippage_rejections=%d avg_latency=%dms max_latency=%dms orders=%d",
                        tick_count,
                        len(tracked_markets),
                        tick.price,
                        pct_chg,
                        price_queue.qsize(),
                        ticks_drained,
                        _executor.slippage_rejections,
                        avg_lat,
                        max_lat,
                        lat_orders,
                    )
                    for cid, tm in tracked_markets.items():
                        for aid in tm.asset_ids:
                            yp = polymarket.get_yes_price(aid)
                            np_ = polymarket.get_no_price(aid)
                            ttl = tm.expiry_time - now
                            age_ms = polymarket.get_price_age_ms(aid)
                            stale_tag = ""
                            if age_ms is not None and age_ms > _MAX_PRICE_AGE_MS:
                                stale_tag = f" STALE({age_ms / 1000:.0f}s)"
                            # Re-seed from REST when WS goes silent.
                            if age_ms is not None and age_ms > _REST_RESEED_AGE_MS:
                                ulog.poly.warning(
                                    "Price stale for %s (age=%ds) — re-seeding from REST",
                                    aid[:16], age_ms // 1000,
                                )
                                await polymarket.seed_book_from_rest(aid, http_session)
                            # Base heartbeat line for this market.
                            ulog.poly.info(
                                " %s | YES=%.3f NO=%.3f | state=%s TTL=%.0fs%s",
                                tm.question[26:50],
                                yp or 0,
                                np_ or 0,
                                tm.state.value,
                                max(ttl, 0),
                                stale_tag,
                            )
                            # Open-position status for P&L observability.
                            if tm.has_open_position and yp is not None:
                                if tm.entry_side and tm.entry_side.upper() == "NO":
                                    # Use real NO price from order book;
                                    # fallback: derive from YES price.
                                    pos_price = np_ if np_ is not None else 1.0 - yp
                                else:
                                    pos_price = yp
                                unrealized_delta = pos_price - tm.entry_price
                                hold_time = now - tm.entry_time
                                ulog.position.info(
                                    "%s | entry=%.4f current=%.4f unrealized_delta=%+.4f held=%.1fs side=%s",
                                    cid[:16],
                                    tm.entry_price,
                                    pos_price,
                                    unrealized_delta,
                                    hold_time,
                                    tm.entry_side,
                                )
                    last_heartbeat = now

                # Log Polymarket prices once per second.
                if now - last_poly_log >= 1.0:
                    for cid, tm in tracked_markets.items():
                        for aid in tm.asset_ids:
                            yp = polymarket.get_yes_price(aid)
                            np_ = polymarket.get_no_price(aid)
                            age_ms = polymarket.get_price_age_ms(aid)
                            age_tag = f" age={age_ms / 1000:.1f}s" if age_ms is not None else ""
                            ulog.poly.info(
                                "%s YES=%.4f NO=%.4f%s",
                                cid[:16],
                                yp or 0,
                                np_ or 0,
                                age_tag,
                            )
                    last_poly_log = now

                await _process_tick(tick, binance, polymarket, tracked_markets, http_session, strategy_config)

            except asyncio.CancelledError:
                raise
            except Exception:
                ulog.startup.exception("Tick processor encountered an error")

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
                ulog.startup.error(
                    "Task %s crashed: %s", task.get_name(), task.exception()
                )

    finally:
        # --- Graceful shutdown --------------------------------------------
        ulog.startup.info("Shutting down updown orchestrator...")

        # Cancel all component tasks.
        for task in tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to finish cancellation.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Close websocket connections and shared HTTP session.
        await polymarket.stop()
        await http_session.close()

        # Log final state.
        ulog.startup.info("Final state: %d tracked markets", len(tracked_markets))
        for cid, tm in tracked_markets.items():
            ulog.startup.info(
                "  %s state=%s question=%s",
                cid[:16],
                tm.state.value,
                tm.question[:60],
            )
        ulog.startup.info("Updown orchestrator stopped.")


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

    ulog.poly.info(
        "Market resolved and removed: %s -- %s (state=%s)",
        condition_id[:16],
        tracked.question[:60],
        tracked.state.value,
    )


async def _rotate_market(
    tracked_markets: dict[str, TrackedMarket],
    polymarket: PolymarketWSClient,
    session: aiohttp.ClientSession,
) -> None:
    """Seed the next 5-minute market when the current one has expired."""
    seeded = await _seed_markets_from_rest(session)
    for condition_id, question, token_id, window_end in seeded:
        if condition_id in tracked_markets:
            continue
        tracked = TrackedMarket(
            condition_id=condition_id,
            question=question,
            asset_ids=[token_id],
            expiry_time=window_end,
            discovered_at=time.time(),
        )
        tracked_markets[condition_id] = tracked
        polymarket.subscribe(token_id)
        await polymarket.seed_book_from_rest(token_id, session)
        ulog.rotate.info(
            "New market: %s — %s (token=%s, TTL=%.0fs)",
            condition_id[:16],
            question[:80],
            token_id[:12],
            window_end - time.time(),
        )


async def _rotate_market_early(
    expiring_market: TrackedMarket,
    tracked_markets: dict[str, TrackedMarket],
    polymarket: PolymarketWSClient,
    session: aiohttp.ClientSession,
) -> None:
    """Proactively seed the next-window market before the current one expires.

    Called when a tracked market's TTL drops below
    ``config.UPDOWN_ROTATION_LEAD_TIME_S``.  The expiring market is NOT
    removed -- it stays tracked and tradeable until its actual TTL reaches 0.
    The new market coexists alongside it during the handoff window.
    """
    seeded = await _seed_next_window_market(session, expiring_market.expiry_time)
    for condition_id, question, token_id, window_end in seeded:
        if condition_id in tracked_markets:
            # Next-window market already tracked -- nothing to do.
            continue
        tracked = TrackedMarket(
            condition_id=condition_id,
            question=question,
            asset_ids=[token_id],
            expiry_time=window_end,
            discovered_at=time.time(),
        )
        tracked_markets[condition_id] = tracked
        polymarket.subscribe(token_id)
        await polymarket.seed_book_from_rest(token_id, session)
        ulog.early_rotate.info(
            "Seeded next-window market: %s — %s (token=%s, TTL=%.0fs)",
            condition_id[:16],
            question[:80],
            token_id[:12],
            window_end - time.time(),
        )


# ---------------------------------------------------------------------------
# TickContext assembly
# ---------------------------------------------------------------------------


def _build_tick_contexts(
    tracked_markets: dict[str, TrackedMarket],
    polymarket: PolymarketWSClient,
    tick_price: float,
    open_price: float,
    exchange_now_ms: int,
    strategy_config: StrategyConfig | None,
) -> dict[str, TickContext]:
    """Gather current prices and assemble a TickContext per tracked market.

    Markets whose primary asset has no YES price yet are silently skipped
    (no TickContext is produced).
    """
    contexts: dict[str, TickContext] = {}

    for condition_id, tracked in tracked_markets.items():
        if not tracked.asset_ids:
            continue
        asset_id = tracked.asset_ids[0]

        yes_price = polymarket.get_yes_price(asset_id)
        if yes_price is None:
            continue

        raw_no_price = polymarket.get_no_price(asset_id)
        no_price = raw_no_price if raw_no_price is not None else 1.0 - yes_price
        price_age_ms = polymarket.get_price_age_ms(asset_id)

        contexts[condition_id] = TickContext(
            tick_price=tick_price,
            tick_timestamp_ms=exchange_now_ms,
            open_price=open_price,
            yes_price=yes_price,
            no_price=no_price,
            price_age_ms=price_age_ms if price_age_ms is not None else 999_999,
            market_id=condition_id,
            question=tracked.question,
            token_id=asset_id,
            expiry_time=tracked.expiry_time,
            state=tracked.state,
            entry_price=tracked.entry_price,
            entry_time=tracked.entry_time,
            entry_side=tracked.entry_side,
            entry_size_usdc=tracked.entry_size_usdc,
            strategy_config=strategy_config,
        )

    return contexts


# ---------------------------------------------------------------------------
# Tick processing
# ---------------------------------------------------------------------------

async def _process_tick(
    tick: PriceUpdate,
    binance: BinanceWS,
    polymarket: PolymarketWSClient,
    tracked_markets: dict[str, TrackedMarket],
    http_session: aiohttp.ClientSession,
    strategy_config: StrategyConfig | None = None,
) -> None:
    """Evaluate every tracked market against the latest BTC price tick.

    Thin orchestrator: gathers prices, calls pure decision functions from
    ``updown.decisions``, then executes side effects (state transitions,
    order placement, rotation).
    """

    open_price = binance.get_window_open_price()
    if open_price is None:
        return

    current_price = tick.price
    exchange_now_ms = get_exchange_now_ms(tick)
    now = exchange_now_ms / 1000.0

    # ── 1. Gather prices and assemble TickContext per market ──────────────
    contexts = _build_tick_contexts(
        tracked_markets, polymarket,
        tick_price=current_price, open_price=open_price,
        exchange_now_ms=exchange_now_ms, strategy_config=strategy_config,
    )

    # ── 1b. Record tick stream (no-op when UPDOWN_TICK_LOG_ENABLED=false) ─
    for ctx in contexts.values():
        _tick_logger.log_tick(ctx)

    # ── 2a. Expiry decisions (pure) ──────────────────────────────────────
    expired_ids = evaluate_expiry(contexts, now)
    for cid in expired_ids:
        _handle_market_resolved(cid, tracked_markets, polymarket)

    if not tracked_markets:
        await _rotate_market(tracked_markets, polymarket, http_session)

    # ── 2b. Early rotation (async HTTP -- stays in orchestrator) ─────────
    lead_time = config.UPDOWN_ROTATION_LEAD_TIME_S
    for _cid, _tm in list(tracked_markets.items()):
        if _tm.expiry_time <= 0:
            continue
        ttl = _tm.expiry_time - now
        if 0 < ttl <= lead_time:
            next_window_end = _tm.expiry_time + 300
            already_seeded = any(
                t.expiry_time == next_window_end
                for t in tracked_markets.values()
            )
            if not already_seeded:
                ulog.early_rotate.info(
                    "Market %s TTL=%.1fs < threshold=%.1fs — seeding next window",
                    _cid[:16], ttl, lead_time,
                )
                await _rotate_market_early(_tm, tracked_markets, polymarket, http_session)

    # ── Tick-only mode: market rotation done, skip trading logic ────────
    if config.UPDOWN_TICK_ONLY:
        return

    # ── 3. Exit decisions (pure) then execute side effects ───────────────
    if strategy_config is not None:
        for condition_id, tracked in list(tracked_markets.items()):
            ctx = contexts.get(condition_id)
            if ctx is None:
                continue

            # Staleness gate: reject prices too old for exit evaluation.
            if ctx.price_age_ms > _MAX_PRICE_AGE_MS:
                ulog.exit.debug(
                    "Skipping %s: stale price (age=%s ms, max=%d ms)",
                    condition_id[:16], ctx.price_age_ms, _MAX_PRICE_AGE_MS,
                )
                continue

            # Determine position-side price (same logic as before).
            if tracked.entry_side and tracked.entry_side.upper() == "NO":
                position_price = ctx.no_price
            else:
                position_price = ctx.yes_price

            exit_signal = evaluate_exit(ctx, position_price, now)
            if exit_signal is None:
                continue

            # ── Execute exit side effects ────────────────────────────
            await _execute_exit(
                tracked, condition_id, exit_signal, position_price,
                ctx, polymarket, now, strategy_config,
            )

    # ── 4. Entry decisions (pure) then execute side effects ──────────────
    for condition_id, tracked in list(tracked_markets.items()):
        # Auto-expire cooldown.
        if tracked.state == MarketState.COOLDOWN and now >= tracked.cooldown_until:
            tracked.state = transition(tracked.state, MarketState.IDLE)

        if tracked.state != MarketState.IDLE:
            if tracked.state == MarketState.ENTERING:
                ulog.entry.debug("Skipping %s: entry order in flight", condition_id[:16])
            continue

        ctx = contexts.get(condition_id)
        if ctx is None:
            continue

        # Staleness gate: reject prices too old for signal computation.
        if ctx.price_age_ms > _MAX_PRICE_AGE_MS:
            ulog.entry.debug(
                "Skipping %s/%s: stale price (age=%s ms, max=%d ms)",
                condition_id[:16], ctx.token_id[:12], ctx.price_age_ms, _MAX_PRICE_AGE_MS,
            )
            continue

        intent = evaluate_entry(
            ctx,
            btc_current=current_price,
            btc_open=open_price,
            threshold=config.UPDOWN_EDGE_THRESHOLD,
            trade_amount_usdc=config.UPDOWN_TRADE_AMOUNT_USDC,
            now=now,
        )
        if intent is None:
            continue

        # ── Execute entry side effects ───────────────────────────
        position_side_price = ctx.no_price if intent.signal.direction == "NO" else ctx.yes_price
        await _execute_entry(
            tracked, condition_id, intent, ctx, position_side_price, now,
            current_price, open_price,
        )


# ---------------------------------------------------------------------------
# Side-effect helpers (keep order placement / state mutation isolated)
# ---------------------------------------------------------------------------


async def _execute_exit(
    tracked: TrackedMarket,
    condition_id: str,
    exit_signal: ExitSignal,
    position_price: float,
    ctx: TickContext,
    polymarket: PolymarketWSClient,
    now: float,
    strategy_config: StrategyConfig,
) -> None:
    """Execute an exit order and apply state transitions."""

    hold_duration_s = now - tracked.entry_time
    delta = position_price - tracked.entry_price
    ulog.exit.info(
        "%s triggered for %s: entry=%.4f current=%.4f delta=%+.4f held=%.1fs",
        exit_signal.reason, condition_id[:16],
        tracked.entry_price, position_price, delta, hold_duration_s,
    )

    exit_no_price = polymarket.get_no_price(ctx.token_id)
    exit_intent = build_exit_intent(
        tracked, exit_signal, ctx.yes_price,
        no_price=exit_no_price,
        tick_timestamp_ms=ctx.tick_timestamp_ms,
    )

    if config.UPDOWN_DRY_MODE:
        ulog.DRY_EXIT.info(
            "Would sell %s %s @ %.4f (entry=%.4f, held=%.1fs, reason=%s)",
            exit_intent.outcome, condition_id[:16],
            position_price, tracked.entry_price,
            hold_duration_s, exit_signal.reason,
        )

    tracked.state = transition(tracked.state, MarketState.EXITING)
    try:
        result: OrderResult = await place_order(
            exit_intent,
            edge=0.0,
            implied_prob=position_price,
            market_price=position_price,
            exit_reason=exit_signal.reason,
            entry_price=tracked.entry_price,
            hold_duration_s=hold_duration_s,
            event_logger=_event_logger,
        )

        if result.success:
            ulog.exit.info(
                "Sell executed for %s: order_id=%s filled_price=%.4f reason=%s held=%.1fs",
                condition_id[:16], result.order_id,
                result.filled_price or 0.0, exit_signal.reason, hold_duration_s,
            )
            tracked.entry_price = None
            tracked.entry_time = None
            tracked.entry_side = None
            tracked.entry_size_usdc = None

            tracked.state = transition(tracked.state, MarketState.COOLDOWN)
            if strategy_config.exit_rules.allow_reentry:
                tracked.cooldown_until = now + _COOLDOWN_SECONDS
            else:
                tracked.cooldown_until = float("inf")
        else:
            ulog.exit.warning("Sell failed for %s: %s", condition_id[:16], result.error)
            tracked.state = transition(tracked.state, MarketState.IDLE)
            tracked.state = transition(tracked.state, MarketState.ENTERING)
            tracked.state = transition(tracked.state, MarketState.ENTERED)

    except Exception:
        ulog.exit.exception("Unexpected error executing exit sell for %s", condition_id[:16])
        tracked.state = transition(tracked.state, MarketState.IDLE)
        tracked.state = transition(tracked.state, MarketState.ENTERING)
        tracked.state = transition(tracked.state, MarketState.ENTERED)


async def _execute_entry(
    tracked: TrackedMarket,
    condition_id: str,
    intent: TradeIntent,
    ctx: TickContext,
    position_side_price: float,
    now: float,
    btc_current: float,
    btc_open: float,
) -> None:
    """Execute an entry order and apply state transitions."""

    sig = intent.signal
    pct_change = (btc_current - btc_open) / btc_open * 100
    ttl = tracked.expiry_time - now
    ulog.poly_binance.debug(
        "%s | YES=%.3f NO=%.3f spread=%.3f | "
        "BTC=%.2f open=%.2f chg=%+.3f%% | "
        "implied=%.3f edge=%+.4f dir=%s trade=%s | TTL=%.0fs",
        tracked.question[26:50],
        ctx.yes_price, ctx.no_price, abs(ctx.yes_price - ctx.no_price),
        btc_current, btc_open, pct_change,
        sig.implied_probability, sig.edge, sig.direction, sig.should_trade,
        max(ttl, 0),
    )
    ulog.signal.info(
        "Triggered for %s: direction=%s edge=%.4f implied=%.4f market=%.4f",
        condition_id[:16], sig.direction, sig.edge,
        sig.implied_probability, sig.market_price,
    )

    tracked.state = transition(tracked.state, MarketState.ENTERING)
    try:
        result: OrderResult = await place_order(
            intent,
            edge=sig.edge,
            implied_prob=sig.implied_probability,
            market_price=position_side_price,
            event_logger=_event_logger,
        )

        if result.success:
            tracked.state = transition(tracked.state, MarketState.ENTERED)
            tracked.entry_price = result.filled_price
            tracked.entry_time = now
            tracked.entry_side = intent.outcome
            tracked.entry_size_usdc = intent.size_usdc
            ulog.poly.info(
                "Trade executed for %s: order_id=%s filled_price=%.4f",
                condition_id[:16], result.order_id, result.filled_price or 0.0,
            )
        else:
            tracked.state = transition(tracked.state, MarketState.IDLE)
            ulog.poly.warning("Trade failed for %s: %s", condition_id[:16], result.error)

    except Exception:
        ulog.entry.exception("Unexpected error executing trade for %s", condition_id[:16])
        tracked.state = transition(tracked.state, MarketState.IDLE)
