"""updown/exit_rules.py — Pure exit-condition evaluator for open positions.

Evaluates take-profit, stop-loss, and time-based exit rules against the
current market state.  Returns an ExitSignal when a rule triggers, or
None when all enabled rules pass.

The function is pure: no I/O, no side effects, fully deterministic for a
given set of inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from updown.strategy_config import ExitRulesConfig


@dataclass(frozen=True)
class ExitSignal:
    """Describes why an exit was triggered."""

    reason: str   # "stop_loss" | "take_profit" | "time_exit"
    detail: str   # Human-readable explanation with numbers


def check_exit(
    config: ExitRulesConfig,
    entry_price: float,
    current_price: float,
    entry_time: float,
    now: float,
    side: str,
) -> Optional[ExitSignal]:
    """Evaluate exit rules against current market state.

    Parameters
    ----------
    config:
        The exit rules portion of the strategy configuration.
    entry_price:
        The price at which the position was entered.
    current_price:
        The latest observed market price.
    entry_time:
        UNIX timestamp when the position was opened.
    now:
        Current UNIX timestamp.
    side:
        ``"YES"`` or ``"NO"`` — determines profit/loss direction.

    Returns
    -------
    ExitSignal if any enabled rule triggers, otherwise None.
    Evaluation order: stop_loss -> take_profit -> time_exit (first match wins).
    """

    # -- Stop loss (checked first) -------------------------------------------
    # NOTE: current_price is always the position-side token price (the caller
    # passes the NO price for NO positions), so profit/loss direction is the
    # same regardless of side: price up = profit, price down = loss.
    if config.stop_loss.enabled:
        max_loss = config.stop_loss.max_loss_delta
        loss = entry_price - current_price

        if loss >= max_loss:
            return ExitSignal(
                reason="stop_loss",
                detail=(
                    f"Stop loss triggered: loss delta {loss:.4f} >= "
                    f"max {max_loss:.4f} (side={side}, "
                    f"entry={entry_price:.4f}, current={current_price:.4f})"
                ),
            )

    # -- Take profit ---------------------------------------------------------
    if config.take_profit.enabled:
        target = config.take_profit.target_delta
        profit = current_price - entry_price

        if profit >= target:
            return ExitSignal(
                reason="take_profit",
                detail=(
                    f"Take profit triggered: profit delta {profit:.4f} >= "
                    f"target {target:.4f} (side={side}, "
                    f"entry={entry_price:.4f}, current={current_price:.4f})"
                ),
            )

    # -- Time exit -----------------------------------------------------------
    if config.time_exit.enabled:
        held = now - entry_time
        max_hold = config.time_exit.max_hold_seconds
        if held >= max_hold:
            return ExitSignal(
                reason="time_exit",
                detail=(
                    f"Time exit triggered: held {held:.1f}s >= "
                    f"max {max_hold:.1f}s"
                ),
            )

    return None
