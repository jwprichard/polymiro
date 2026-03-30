"""Polymarket CLOB REST trade executor.

Provides ``place_order`` — an async function that either submits a real
limit order via the ``py-clob-client`` library or, in dry mode, logs the
intent and returns a synthetic ``OrderResult`` with ``status="dry"``.

Every executed (or dry-run) trade is persisted to ``UPDOWN_TRADES_FILE``
via the atomic append utility so the monitor and dashboard can replay
the full trade history.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import BUY, SELL

import config
from updown.exit_rules import ExitSignal
from updown.types import MarketSnapshot, OrderResult, SignalResult, TradeIntent
from utils.io import atomic_append_to_json_list

if TYPE_CHECKING:
    from updown.loop import TrackedMarket

logger = logging.getLogger(__name__)

# Polygon mainnet chain ID — required by py-clob-client for signing.
_POLYGON_CHAIN_ID = 137

# Default HTTP timeout for order submission (seconds).
_REQUEST_TIMEOUT_S = 10

# Exit trades use a wider slippage tolerance to prioritise closing
# positions over price precision.
_EXIT_SLIPPAGE_MULTIPLIER: float = 2.0

# Module-level counter for dashboard / heartbeat visibility.
slippage_rejections: int = 0

# Rolling latency samples accumulated between heartbeat intervals.
# Each entry is one tick_to_order_latency_ms measurement.
_latency_samples: list[int] = []


def record_latency_sample(latency_ms: int) -> None:
    """Append a latency measurement for heartbeat reporting."""
    _latency_samples.append(latency_ms)


def drain_latency_stats() -> tuple[int, int, int]:
    """Drain accumulated latency samples and return (avg_ms, max_ms, count).

    Resets the internal buffer so each heartbeat interval starts fresh.
    Returns (0, 0, 0) when no samples have been recorded.
    """
    if not _latency_samples:
        return 0, 0, 0
    avg_ms = sum(_latency_samples) // len(_latency_samples)
    max_ms = max(_latency_samples)
    count = len(_latency_samples)
    _latency_samples.clear()
    return avg_ms, max_ms, count


class ExecutorError(Exception):
    """Raised when order placement fails in a recoverable way."""


# ---------------------------------------------------------------------------
# Slippage protection
# ---------------------------------------------------------------------------


def check_slippage(
    signal_price: float,
    execution_price: float,
    tolerance: float,
) -> bool:
    """Return True when slippage exceeds *tolerance*.

    This is a pure function with no side effects — it only compares the
    absolute price delta against the tolerance threshold.

    Parameters
    ----------
    signal_price:
        The market price at the time the signal was generated.
    execution_price:
        The market price at the time the order would be submitted.
    tolerance:
        Maximum acceptable absolute delta between the two prices.

    Returns
    -------
    bool
        ``True`` if ``abs(signal_price - execution_price) > tolerance``,
        i.e. slippage is excessive and the order should be rejected.
    """
    return abs(signal_price - execution_price) > tolerance


# ---------------------------------------------------------------------------
# Module-level client cache
# ---------------------------------------------------------------------------

_clob_client: Optional[ClobClient] = None


def _get_clob_client() -> ClobClient:
    """Return a reusable ``ClobClient`` configured from ``config.py``.

    The client is instantiated at Level 2 (full auth) using the API key,
    secret, and passphrase from the environment.  It is cached at module
    level so the expensive setup (nonce derivation, etc.) happens once.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    key = config.POLYMARKET_API_KEY
    secret = config.POLYMARKET_API_SECRET
    passphrase = config.POLYMARKET_API_PASSPHRASE

    if not all([key, secret, passphrase]):
        raise ExecutorError(
            "Live trading requires POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
            "and POLYMARKET_API_PASSPHRASE to be set in the environment."
        )

    creds = ApiCreds(
        api_key=key,
        api_secret=secret,
        api_passphrase=passphrase,
    )

    _clob_client = ClobClient(
        host=config.POLYMARKET_CLOB_REST_URL,
        chain_id=_POLYGON_CHAIN_ID,
        key=key,
        creds=creds,
    )
    return _clob_client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_exit_intent(
    tracked: TrackedMarket,
    exit_signal: ExitSignal,
    current_price: float,
    *,
    no_price: Optional[float] = None,
    tick_timestamp_ms: int = 0,
) -> TradeIntent:
    """Construct a sell-side TradeIntent from an open position and its exit signal.

    Parameters
    ----------
    tracked:
        The TrackedMarket with an open position (entry_* fields populated).
    exit_signal:
        The ExitSignal that triggered the exit (provides reason/detail).
    current_price:
        The current YES price at which the sell will be submitted.
    no_price:
        Real NO price from the order book.  When ``None``, falls back to
        ``1.0 - current_price`` (explicit fallback for when the NO token
        is not subscribed or the book is empty).

    Returns
    -------
    TradeIntent
        A fully populated sell-side intent ready for ``place_order``.

    Raises
    ------
    ValueError
        If the tracked market does not have a recorded open position.
    """
    if not tracked.has_open_position:
        raise ValueError(
            f"Cannot build exit intent: market {tracked.condition_id} "
            "has no open position (entry_price is None)"
        )

    # The token_id is the first (and currently only) asset for the market.
    token_id = tracked.asset_ids[0]

    # Fallback: derive NO price from YES when the caller does not
    # supply a real order-book NO price.
    resolved_no_price = no_price if no_price is not None else 1.0 - current_price

    # Build a minimal MarketSnapshot for the exit.
    snapshot = MarketSnapshot(
        market_id=tracked.condition_id,
        question=tracked.question,
        token_id=token_id,
        yes_price=current_price,
        no_price=resolved_no_price,
        spread=abs(current_price - resolved_no_price),
        timestamp_ms=int(time.time() * 1000),
    )

    # Build a minimal SignalResult for the exit (no momentum computation).
    signal = SignalResult(
        direction=tracked.entry_side.upper() if tracked.entry_side else "YES",
        implied_probability=current_price,
        market_price=current_price,
        edge=0.0,
        should_trade=True,
    )

    return TradeIntent(
        market_id=tracked.condition_id,
        token_id=token_id,
        side="sell",
        outcome=tracked.entry_side or "yes",  # same outcome token we bought
        size_usdc=tracked.entry_size_usdc or 0.0,
        signal=signal,
        market=snapshot,
        reason=f"EXIT ({exit_signal.reason}): {exit_signal.detail}",
        signal_price=resolved_no_price if (tracked.entry_side and tracked.entry_side.upper() == "NO") else current_price,
        tick_timestamp_ms=tick_timestamp_ms,
    )


async def place_order(
    intent: TradeIntent,
    *,
    edge: float = 0.0,
    implied_prob: float = 0.0,
    market_price: float = 0.0,
    timeout_s: float = _REQUEST_TIMEOUT_S,
    exit_reason: Optional[str] = None,
    entry_price: Optional[float] = None,
    hold_duration_s: Optional[float] = None,
    exchange_timestamp_ms: Optional[int] = None,
) -> OrderResult:
    """Place (or dry-run) an aggressive limit order on the Polymarket CLOB.

    Parameters
    ----------
    intent:
        Fully populated ``TradeIntent`` describing what to trade.
    edge:
        Signed edge value (our probability minus market price).
    implied_prob:
        Our model's implied probability for the outcome.
    market_price:
        Current market price of the outcome token.
    timeout_s:
        Maximum seconds to wait for the CLOB API response.

    Returns
    -------
    OrderResult
        Populated with either the live fill data or a synthetic dry-run
        result.

    Notes
    -----
    *   In dry mode (``UPDOWN_DRY_MODE=true``, the default) the order is
        never sent to Polymarket.  A synthetic ``OrderResult`` with
        ``order_id`` prefixed ``dry-`` is returned instead.
    *   In live mode, the order is constructed and posted via
        ``py-clob-client``'s ``create_and_post_order`` (a synchronous
        call wrapped in ``asyncio.to_thread`` so it does not block the
        event loop).
    *   Aggressive limit orders cross the spread — the price is set
        equal to the best opposing quote — to maximise fill probability
        in v1.
    """
    now_ms = int(time.time() * 1000)
    trade_id = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Tick-to-order latency measurement
    # ------------------------------------------------------------------
    tick_to_order_latency_ms: int = 0
    if intent.tick_timestamp_ms > 0:
        tick_to_order_latency_ms = now_ms - intent.tick_timestamp_ms
    logger.info("[LATENCY] tick_to_order=%dms", tick_to_order_latency_ms)
    record_latency_sample(tick_to_order_latency_ms)

    # ------------------------------------------------------------------
    # Slippage guard — reject if price moved too far since signal time
    # ------------------------------------------------------------------
    if intent.signal_price is not None and market_price > 0:
        global slippage_rejections
        tolerance = config.UPDOWN_SLIPPAGE_TOLERANCE
        if intent.side == "sell":
            tolerance *= _EXIT_SLIPPAGE_MULTIPLIER
        if check_slippage(intent.signal_price, market_price, tolerance):
            delta = abs(intent.signal_price - market_price)
            logger.warning(
                "Slippage rejected %s %s %s: signal_price=%.4f execution_price=%.4f "
                "delta=%.4f tolerance=%.4f",
                intent.side,
                intent.outcome,
                intent.token_id[:12],
                intent.signal_price,
                market_price,
                delta,
                tolerance,
            )
            slippage_rejections += 1
            return OrderResult(
                intent=intent,
                success=False,
                error=f"slippage exceeded: delta={delta:.4f} > tolerance={tolerance:.4f}",
                timestamp_ms=now_ms,
            )

    # ------------------------------------------------------------------
    # Dry mode — no network call, immediate synthetic result
    # ------------------------------------------------------------------
    if config.UPDOWN_DRY_MODE:
        logger.info(
            "[DRY] %s %s %s %.4f USDC @ %.4f  (edge=%.4f)",
            intent.side,
            intent.outcome,
            intent.token_id[:12],
            intent.size_usdc,
            market_price,
            edge,
        )
        result = OrderResult(
            intent=intent,
            success=True,
            order_id=f"dry-{trade_id}",
            filled_price=market_price,
            filled_size=intent.size_usdc,
            error=None,
            timestamp_ms=now_ms,
        )
        _persist_trade(
            trade_id, intent, result, edge, implied_prob, market_price,
            dry=True,
            exit_reason=exit_reason,
            entry_price=entry_price,
            exit_price=market_price if intent.side == "sell" else None,
            hold_duration_s=hold_duration_s,
            realized_delta=_compute_realized_delta(
                intent.outcome, entry_price, market_price
            ) if intent.side == "sell" and entry_price is not None else None,
            exchange_timestamp_ms=exchange_timestamp_ms,
            tick_to_order_latency_ms=tick_to_order_latency_ms,
        )
        return result

    # ------------------------------------------------------------------
    # Live mode — submit via py-clob-client
    # ------------------------------------------------------------------
    try:
        client = _get_clob_client()

        side_const = BUY if intent.side.lower() == "buy" else SELL

        order_args = OrderArgs(
            token_id=intent.token_id,
            price=market_price,
            size=intent.size_usdc,
            side=side_const,
        )

        # py-clob-client is synchronous; run in a thread to keep the
        # event loop responsive.
        response = await asyncio.wait_for(
            asyncio.to_thread(client.create_and_post_order, order_args),
            timeout=timeout_s,
        )

        # The response from create_and_post_order is a dict with fields
        # like {"orderID": "...", "status": "matched", ...} on success.
        order_id = (
            response.get("orderID")
            or response.get("order_id")
            or response.get("id")
            or "unknown"
        )
        status = response.get("status", "unknown")
        success = status in ("matched", "live", "delayed")

        result = OrderResult(
            intent=intent,
            success=success,
            order_id=order_id,
            filled_price=market_price,
            filled_size=intent.size_usdc if success else 0.0,
            error=None if success else f"CLOB status: {status}",
            timestamp_ms=now_ms,
        )

        logger.info(
            "[LIVE] %s %s %s %.4f USDC @ %.4f → %s (%s)",
            intent.side,
            intent.outcome,
            intent.token_id[:12],
            intent.size_usdc,
            market_price,
            order_id,
            status,
        )

    except ExecutorError:
        # Missing credentials — let it propagate so the caller knows
        # the system is misconfigured rather than experiencing a
        # transient failure.
        raise

    except asyncio.TimeoutError:
        logger.error(
            "Order timed out after %.1fs for %s %s %s",
            timeout_s,
            intent.side,
            intent.outcome,
            intent.token_id[:12],
        )
        result = OrderResult(
            intent=intent,
            success=False,
            error=f"Timeout after {timeout_s}s",
            timestamp_ms=now_ms,
        )

    except Exception as exc:
        # Catch-all for HTTP 4xx/5xx, network errors, JSON decode
        # failures, or any other transient issue.  We log and return
        # a failed OrderResult instead of letting the exception
        # propagate and crash the event loop.
        logger.error(
            "Order failed for %s %s %s: %s",
            intent.side,
            intent.outcome,
            intent.token_id[:12],
            exc,
            exc_info=True,
        )
        result = OrderResult(
            intent=intent,
            success=False,
            error=str(exc)[:500],
            timestamp_ms=now_ms,
        )

    _persist_trade(
        trade_id, intent, result, edge, implied_prob, market_price,
        dry=False,
        exit_reason=exit_reason,
        entry_price=entry_price,
        exit_price=market_price if intent.side == "sell" else None,
        hold_duration_s=hold_duration_s,
        realized_delta=_compute_realized_delta(
            intent.outcome, entry_price, market_price
        ) if intent.side == "sell" and entry_price is not None else None,
        exchange_timestamp_ms=exchange_timestamp_ms,
        tick_to_order_latency_ms=tick_to_order_latency_ms,
    )
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _compute_realized_delta(
    outcome: str,
    entry_price: Optional[float],
    exit_price: float,
) -> Optional[float]:
    """Compute the realized P&L delta for a closed position.

    For YES side: profit = exit_price - entry_price
    For NO side:  profit = entry_price - exit_price
    """
    if entry_price is None:
        return None
    if outcome.lower() == "yes":
        return exit_price - entry_price
    else:
        return entry_price - exit_price


def _persist_trade(
    trade_id: str,
    intent: TradeIntent,
    result: OrderResult,
    edge: float,
    implied_prob: float,
    market_price: float,
    *,
    dry: bool,
    exit_reason: Optional[str] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    hold_duration_s: Optional[float] = None,
    realized_delta: Optional[float] = None,
    exchange_timestamp_ms: Optional[int] = None,
    tick_to_order_latency_ms: int = 0,
) -> None:
    """Append a complete trade record to ``UPDOWN_TRADES_FILE``.

    Parameters
    ----------
    exit_reason:
        For sell trades: "take_profit", "stop_loss", or "time_exit".
        Omitted (None) for buy trades.
    entry_price:
        For sell trades: the price at which the position was originally entered.
    exit_price:
        For sell trades: the price at which the position was closed.
    hold_duration_s:
        For sell trades: seconds between entry and exit.
    realized_delta:
        For sell trades: profit/loss expressed as price delta
        (exit_price - entry_price for YES side, entry_price - exit_price for NO side).
    tick_to_order_latency_ms:
        Milliseconds between the exchange tick timestamp and the order
        submission wall-clock time.
    """
    record: dict[str, object] = {
        "trade_id": trade_id,
        "asset_id": intent.token_id,
        "direction": intent.side,
        "edge": round(edge, 6),
        "implied_prob": round(implied_prob, 6),
        "market_price": round(market_price, 6),
        "amount_usdc": intent.size_usdc,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "exchange_timestamp_ms": exchange_timestamp_ms,
        "tick_to_order_latency_ms": tick_to_order_latency_ms,
        "status": "dry" if dry else ("filled" if result.success else "failed"),
        "dry_mode": dry,
        "order_id": result.order_id,
        "outcome": intent.outcome,
        "market_id": intent.market_id,
    }

    # Sell-side enrichment for P&L analysis.
    if intent.side == "sell":
        record["exit_reason"] = exit_reason
        if entry_price is not None:
            record["entry_price"] = round(entry_price, 6)
        if exit_price is not None:
            record["exit_price"] = round(exit_price, 6)
        if hold_duration_s is not None:
            record["hold_duration_s"] = round(hold_duration_s, 2)
        if realized_delta is not None:
            record["realized_delta"] = round(realized_delta, 6)

    try:
        atomic_append_to_json_list(config.UPDOWN_TRADES_FILE, record)
    except Exception:
        # Persistence failure must not crash the trading loop.
        logger.exception("Failed to persist trade record %s", trade_id)

    # Compute and persist P&L immediately for exit trades.
    if intent.side == "sell" and entry_price is not None and exit_price is not None:
        _persist_exit_pnl(trade_id, intent, entry_price, exit_price, exit_reason, hold_duration_s)


def _persist_exit_pnl(
    trade_id: str,
    intent: TradeIntent,
    entry_price: float,
    exit_price: float,
    exit_reason: Optional[str],
    hold_duration_s: Optional[float],
) -> None:
    """Compute P&L for a completed exit and append to the PnL report."""
    amount = intent.size_usdc
    shares = round(amount / entry_price, 6)
    exit_value = round(shares * exit_price, 6)
    gross_pnl = round(exit_value - amount, 6)
    fee = round(config.PNL_FEE_RATE * gross_pnl, 6) if gross_pnl > 0 else 0.0
    net_pnl = round(gross_pnl - fee, 6)

    pnl_record = {
        "trade_id": trade_id,
        "market_id": intent.market_id,
        "outcome_bet": intent.outcome.upper(),
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "amount_usdc": round(amount, 6),
        "shares": shares,
        "payout": exit_value,
        "gross_pnl": gross_pnl,
        "fee": fee,
        "net_pnl": net_pnl,
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "updown",
        "settlement_type": "exit",
        "exit_reason": exit_reason,
        "hold_duration_s": hold_duration_s,
    }

    try:
        atomic_append_to_json_list(config.PNL_REPORT_FILE, pnl_record)
        logger.info(
            "[P&L] %s %s: entry=%.4f exit=%.4f shares=%.2f net=%+.4f USDC (%s)",
            intent.outcome.upper(),
            intent.market_id[:16],
            entry_price,
            exit_price,
            shares,
            net_pnl,
            exit_reason,
        )
    except Exception:
        logger.exception("Failed to persist P&L record %s", trade_id)
