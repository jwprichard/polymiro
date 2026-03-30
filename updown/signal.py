"""Momentum signal engine -- pure numeric computation, no I/O.

Converts a BTC price movement (current vs. open) into a Polymarket
trading signal by deriving an implied probability from the percentage
change, then comparing it to the live market YES price to find edge.

Usage:
    from updown.signal import compute_signal
    result = compute_signal(
        current_price=67_450.0,
        open_price=67_200.0,
        market_yes_price=0.52,
        threshold=0.05,
    )
"""

from common import config
from common.log import ulog
from updown.types import SignalResult

# ---------------------------------------------------------------------------
# Scale factor: maps a BTC percentage move to a probability shift.
#
# Sourced from config.UPDOWN_SCALE_FACTOR (overridable via the
# UPDOWN_SCALE_FACTOR env var).  Default: 0.01.
#
# A pct_change equal to SCALE_FACTOR drives the implied probability from
# 0.5 to the clamp boundary (maximum conviction).  With the default of
# 0.01, a ~0.03% BTC move produces an edge of ~0.05 against a 0.50 market
# price — just enough to clear the default UPDOWN_EDGE_THRESHOLD.
#
# Tune this value based on the observation window configured in
# config.UPDOWN_WINDOW_SECONDS.  Longer windows should use a larger factor;
# shorter windows a smaller one.
# ---------------------------------------------------------------------------
SCALE_FACTOR: float = config.UPDOWN_SCALE_FACTOR

# ---------------------------------------------------------------------------
# Minimum BTC percentage change gate.
#
# Sourced from config.UPDOWN_MIN_BTC_PCT_CHANGE (overridable via the
# UPDOWN_MIN_BTC_PCT_CHANGE env var).  Default: 0.0001 (0.01%).
#
# When the absolute percentage change is below this threshold the signal
# engine returns should_trade=False regardless of edge, preventing noisy
# micro-moves from producing trade signals.
# ---------------------------------------------------------------------------
MIN_BTC_PCT_CHANGE: float = config.UPDOWN_MIN_BTC_PCT_CHANGE


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to the closed interval [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def compute_signal(
    current_price: float,
    open_price: float,
    market_yes_price: float,
    threshold: float,
) -> SignalResult:
    """Derive a Polymarket YES/NO signal from BTC price momentum.

    Parameters
    ----------
    current_price:
        Latest BTC/USDT price.
    open_price:
        BTC/USDT price at the start of the observation window.
        Must be > 0.
    market_yes_price:
        Current Polymarket YES token price in [0, 1].
    threshold:
        Minimum absolute edge required for ``should_trade`` to be True.

    Returns
    -------
    SignalResult
        A deterministic, pure-data result with no side effects.

    Raises
    ------
    ValueError
        If *open_price* is not positive.
    """
    if open_price <= 0:
        raise ValueError(f"open_price must be positive, got {open_price}")

    # Percentage change as a decimal (e.g. +0.002 = +0.2%).
    pct_change: float = (current_price - open_price) / open_price

    # Map percentage change to an implied probability.
    # Positive pct_change -> probability > 0.5 (BTC went up -> YES more likely).
    # Negative pct_change -> probability < 0.5 (BTC went down -> NO more likely).
    implied_probability: float = _clamp(
        0.5 + (pct_change / SCALE_FACTOR),
        0.01,
        0.99,
    )

    # Compute edge for both sides.
    market_no_price: float = 1.0 - market_yes_price
    yes_edge: float = implied_probability - market_yes_price
    no_edge: float = (1.0 - implied_probability) - market_no_price

    # Pick the side with a positive edge.  When both are non-positive
    # (should only happen at exact equilibrium), default to the side
    # whose edge is closer to zero -- but should_trade will be False
    # anyway because abs(edge) will be tiny.
    if yes_edge >= no_edge:
        direction = "YES"
        edge = yes_edge
    else:
        direction = "NO"
        edge = no_edge

    should_trade: bool = abs(edge) > threshold

    # --- Minimum BTC percentage change gate -------------------------------
    # Reject ticks where the BTC move is too small to be meaningful.
    # Direction and edge are preserved for logging; only should_trade is
    # forced to False so callers can still observe the math.
    if abs(pct_change) < MIN_BTC_PCT_CHANGE:
        ulog.signal.debug(
            "BTC pct_change %.6f%% below min gate %.4f%% — skipping tick "
            "(direction=%s edge=%+.4f implied=%.4f)",
            pct_change * 100,
            MIN_BTC_PCT_CHANGE * 100,
            direction,
            edge,
            implied_probability,
        )
        should_trade = False

    return SignalResult(
        direction=direction,
        implied_probability=implied_probability,
        market_price=market_yes_price,
        edge=edge,
        should_trade=should_trade,
    )
