"""updown/decisions.py -- Pure decision functions extracted from _process_tick.

Each function accepts only plain data (floats, strings, dataclasses) and
returns a decision object.  No WS client, no aiohttp session, no I/O of
any kind.  All three functions can be called from a synchronous test
harness with synthetic data.

Imports are restricted to:
    updown.types, updown.signal, updown.exit_rules
"""

from __future__ import annotations

import logging
from typing import Optional

from updown.exit_rules import ExitSignal, check_exit
from updown.signal import compute_signal
from updown.types import (
    MarketSnapshot,
    MarketState,
    SignalResult,
    TickContext,
    TradeIntent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# evaluate_expiry
# ---------------------------------------------------------------------------


def evaluate_expiry(
    tracked_markets: dict[str, TickContext],
    now: float,
) -> list[str]:
    """Return condition_ids whose markets have expired.

    A market is expired when its ``expiry_time`` is positive and strictly
    less than *now*.  The caller is responsible for pruning/unsubscribing.

    Parameters
    ----------
    tracked_markets:
        Mapping of condition_id to TickContext (or any object with an
        ``expiry_time`` float attribute).
    now:
        Current UNIX timestamp in seconds.

    Returns
    -------
    List of condition_id strings that should be pruned.
    """
    return [
        cid
        for cid, ctx in tracked_markets.items()
        if ctx.expiry_time > 0 and now > ctx.expiry_time
    ]


# ---------------------------------------------------------------------------
# evaluate_exit
# ---------------------------------------------------------------------------


def evaluate_exit(
    ctx: TickContext,
    position_price: float,
    now: float,
) -> Optional[ExitSignal]:
    """Evaluate whether an open position should be exited.

    Applies staleness gating and state guards before delegating to
    :func:`updown.exit_rules.check_exit`.

    Parameters
    ----------
    ctx:
        Immutable tick context for this market.  Must have
        ``state == MarketState.ENTERED`` and populated entry fields.
    position_price:
        The current price from the position's perspective (YES price for
        YES positions, NO price for NO positions).
    now:
        Current UNIX timestamp in seconds.

    Returns
    -------
    An :class:`ExitSignal` if an exit rule triggers, otherwise ``None``.
    Returns ``None`` immediately when:
    - the market is not in ENTERED state,
    - the market is already in EXITING state,
    - entry fields are missing, or
    - the strategy config has no exit rules configured.
    """
    # Guard: only ENTERED positions are eligible for exit evaluation.
    if ctx.state != MarketState.ENTERED:
        return None

    # Guard: entry fields must be populated.
    if ctx.entry_price is None or ctx.entry_time is None:
        return None

    side = ctx.entry_side.upper() if ctx.entry_side else "YES"

    return check_exit(
        config=ctx.strategy_config.exit_rules,
        entry_price=ctx.entry_price,
        current_price=position_price,
        entry_time=ctx.entry_time,
        now=now,
        side=side,
    )


# ---------------------------------------------------------------------------
# evaluate_entry
# ---------------------------------------------------------------------------


def evaluate_entry(
    ctx: TickContext,
    btc_current: float,
    btc_open: float,
    threshold: float,
    trade_amount_usdc: float,
    now: float,
) -> Optional[TradeIntent]:
    """Evaluate whether a new position should be opened.

    Runs the momentum signal computation and, if the signal says to trade,
    builds a :class:`TradeIntent`.  No order is placed -- the caller is
    responsible for execution.

    Parameters
    ----------
    ctx:
        Immutable tick context for this market.
    btc_current:
        Latest BTC/USDT price from Binance.
    btc_open:
        BTC/USDT price at the start of the observation window.
    threshold:
        Minimum absolute edge for ``should_trade`` to be True.
    trade_amount_usdc:
        Position size in USDC for the trade intent.
    now:
        Current UNIX timestamp in seconds.

    Returns
    -------
    A :class:`TradeIntent` if the signal fires, otherwise ``None``.
    Returns ``None`` immediately when:
    - the market is not in IDLE state,
    - ``btc_open`` is not positive.
    """
    # Guard: only IDLE markets are eligible for new entries.
    if ctx.state != MarketState.IDLE:
        return None

    # Guard: need a valid open price.
    if btc_open <= 0:
        return None

    try:
        sig: SignalResult = compute_signal(
            current_price=btc_current,
            open_price=btc_open,
            market_yes_price=ctx.yes_price,
            threshold=threshold,
        )
    except ValueError:
        return None

    if not sig.should_trade:
        return None

    # Build trade intent -- mirrors the logic in _process_tick.
    position_side_price = ctx.no_price if sig.direction == "NO" else ctx.yes_price

    snapshot = MarketSnapshot(
        market_id=ctx.market_id,
        question=ctx.question,
        token_id=ctx.token_id,
        yes_price=ctx.yes_price,
        no_price=ctx.no_price,
        spread=abs(ctx.yes_price - ctx.no_price),
        timestamp_ms=ctx.tick_timestamp_ms,
    )

    return TradeIntent(
        market_id=ctx.market_id,
        token_id=ctx.token_id,
        side="buy",
        outcome=sig.direction.lower(),  # "yes" or "no"
        size_usdc=trade_amount_usdc,
        signal=sig,
        market=snapshot,
        reason=(
            f"BTC {sig.direction} momentum | edge={sig.edge:+.4f} "
            f"implied={sig.implied_probability:.4f} "
            f"market={sig.market_price:.4f}"
        ),
        signal_price=position_side_price,
        tick_timestamp_ms=ctx.tick_timestamp_ms,
    )
