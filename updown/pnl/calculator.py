"""P&L calculation engine for resolved Polymarket trades.

Given a trade record and the winning outcome of a resolved market, computes
the full profit-and-loss breakdown: shares acquired, payout, gross P&L, fees,
and net P&L.

Key formulas
------------
Share calculation (entry):
    shares = amount_usdc / entry_price
    (entry_price is the position-side token price: YES price for YES bets, NO price for NO bets)

Settlement (resolution):
    Winning trade:  payout = shares * 1.0
                    gross_pnl = payout - amount_usdc
                    fee = PNL_FEE_RATE * gross_pnl
                    net_pnl = gross_pnl - fee
    Losing trade:   payout = 0
                    gross_pnl = -amount_usdc
                    fee = 0
                    net_pnl = -amount_usdc

All monetary values are rounded to 6 decimal places.

Public API
----------
calculate_pnl(trade, winning_outcome) -> dict
calculate_exit_pnl(trade) -> dict
"""

from __future__ import annotations

from datetime import datetime, timezone

from common import config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DECIMAL_PLACES = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round_money(value: float) -> float:
    """Round a monetary value to the standard number of decimal places."""
    return round(value, _DECIMAL_PLACES)


def _normalise_outcome(raw: str) -> str:
    """Normalise an outcome string to uppercase YES/NO.

    Accepts common representations: "yes", "YES", "Yes", "no", "NO", "No".
    Returns "YES" or "NO".

    Raises
    ------
    ValueError
        If the string is not a recognised outcome value.
    """
    upper = raw.strip().upper()
    if upper not in ("YES", "NO"):
        raise ValueError(
            f"Unrecognised outcome value '{raw}'. Expected 'YES' or 'NO'."
        )
    return upper


def _extract_entry_price(trade: dict) -> float:
    """Extract the entry price from a trade record.

    Supports both naming conventions used across the codebase:
    - ``entry_price``   (canonical for P&L records)
    - ``market_price``  (used by the updown strategy)

    Raises
    ------
    KeyError
        If neither field is present.
    ValueError
        If the price is not in the valid (0, 1) open interval.
    """
    price: float | None = trade.get("entry_price")
    if price is None:
        price = trade.get("market_price")
    if price is None:
        raise KeyError(
            "Trade record must contain 'entry_price' or 'market_price'."
        )
    price = float(price)
    if not (0.0 < price < 1.0):
        raise ValueError(
            f"Entry price must be in the open interval (0, 1), got {price}."
        )
    return price


def _detect_source(trade: dict) -> str:
    """Infer the trade source from the record.

    Returns ``"updown"`` if the trade contains updown-specific keys,
    otherwise ``"research"``.
    """
    if trade.get("source"):
        return str(trade["source"])
    # Updown trades always carry a ``signal`` or ``token_id`` key.
    if "signal" in trade or "token_id" in trade:
        return "updown"
    return "research"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_pnl(trade: dict, winning_outcome: str) -> dict:
    """Compute the P&L record for a single resolved trade.

    Parameters
    ----------
    trade:
        A trade record dict.  Required keys:
        - ``trade_id``   (str)
        - ``market_id``  (str)
        - ``direction`` or ``outcome_bet`` (str, "YES" or "NO")
        - ``entry_price`` or ``market_price`` (float, in (0, 1))
        - ``amount_usdc`` (float, > 0)

    winning_outcome:
        The resolved outcome of the market: ``"YES"`` or ``"NO"``.

    Returns
    -------
    dict
        A P&L record with the following keys:
        ``trade_id``, ``market_id``, ``outcome_bet``, ``winning_outcome``,
        ``entry_price``, ``amount_usdc``, ``shares``, ``payout``,
        ``gross_pnl``, ``fee``, ``net_pnl``, ``resolved_at`` (ISO timestamp),
        ``source`` ("research" or "updown").

    Raises
    ------
    KeyError
        If a required field is missing from *trade*.
    ValueError
        If a field value is outside its valid domain.
    """
    # --- Normalise inputs ---------------------------------------------------

    winning_outcome = _normalise_outcome(winning_outcome)

    trade_id: str = trade["trade_id"]
    market_id: str = trade["market_id"]

    # Accept both ``direction`` (trade executor) and ``outcome_bet`` (P&L).
    outcome_bet_raw: str | None = trade.get("outcome_bet") or trade.get("direction")
    if outcome_bet_raw is None:
        raise KeyError(
            "Trade record must contain 'outcome_bet' or 'direction'."
        )
    outcome_bet: str = _normalise_outcome(outcome_bet_raw)

    entry_price: float = _extract_entry_price(trade)
    amount_usdc: float = float(trade["amount_usdc"])
    if amount_usdc <= 0:
        raise ValueError(
            f"amount_usdc must be positive, got {amount_usdc}."
        )

    # --- Share calculation ---------------------------------------------------
    # Prices are position-side: YES prices for YES bets, NO prices for NO bets.

    shares = amount_usdc / entry_price

    shares = _round_money(shares)

    # --- Settlement ----------------------------------------------------------

    won = outcome_bet == winning_outcome

    if won:
        payout = _round_money(shares * 1.0)
        gross_pnl = _round_money(payout - amount_usdc)
        fee = _round_money(config.PNL_FEE_RATE * gross_pnl)
        net_pnl = _round_money(gross_pnl - fee)
    else:
        payout = 0.0
        gross_pnl = _round_money(-amount_usdc)
        fee = 0.0
        net_pnl = _round_money(-amount_usdc)

    # --- Build result --------------------------------------------------------

    return {
        "trade_id": trade_id,
        "market_id": market_id,
        "outcome_bet": outcome_bet,
        "winning_outcome": winning_outcome,
        "entry_price": _round_money(entry_price),
        "amount_usdc": _round_money(amount_usdc),
        "shares": shares,
        "payout": payout,
        "gross_pnl": gross_pnl,
        "fee": fee,
        "net_pnl": net_pnl,
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": _detect_source(trade),
        "settlement_type": "resolution",
    }


def calculate_exit_pnl(trade: dict) -> dict:
    """Compute P&L for a trade closed via a sell/exit order.

    Unlike :func:`calculate_pnl` (binary resolution), this settles at the
    actual exit price recorded in the trade.

    Parameters
    ----------
    trade:
        A sell-record dict.  Required keys:
        ``trade_id``, ``market_id``, ``outcome`` or ``outcome_bet``,
        ``entry_price``, ``exit_price``, ``amount_usdc``, ``exit_reason``.

    Returns
    -------
    dict
        A P&L record compatible with :func:`calculate_pnl` output, plus
        ``exit_price``, ``exit_reason``, ``hold_duration_s``, and
        ``settlement_type`` (``"exit"``).
    """
    trade_id: str = trade["trade_id"]
    market_id: str = trade["market_id"]

    outcome_bet_raw: str | None = trade.get("outcome_bet") or trade.get("outcome")
    if outcome_bet_raw is None:
        raise KeyError("Trade record must contain 'outcome_bet' or 'outcome'.")
    outcome_bet: str = _normalise_outcome(outcome_bet_raw)

    entry_price: float = _extract_entry_price(trade)

    exit_price = trade.get("exit_price")
    if exit_price is None:
        raise KeyError("Exit trade must contain 'exit_price'.")
    exit_price = float(exit_price)

    amount_usdc: float = float(trade["amount_usdc"])
    if amount_usdc <= 0:
        raise ValueError(f"amount_usdc must be positive, got {amount_usdc}.")

    # --- Share & exit value ----------------------------------------------------
    # Prices are position-side: YES prices for YES bets, NO prices for NO bets.

    shares = amount_usdc / entry_price
    exit_value = shares * exit_price

    shares = _round_money(shares)
    exit_value = _round_money(exit_value)

    gross_pnl = _round_money(exit_value - amount_usdc)
    fee = _round_money(config.PNL_FEE_RATE * gross_pnl) if gross_pnl > 0 else 0.0
    net_pnl = _round_money(gross_pnl - fee)

    return {
        "trade_id": trade_id,
        "market_id": market_id,
        "outcome_bet": outcome_bet,
        "entry_price": _round_money(entry_price),
        "exit_price": _round_money(exit_price),
        "amount_usdc": _round_money(amount_usdc),
        "shares": shares,
        "payout": exit_value,
        "gross_pnl": gross_pnl,
        "fee": fee,
        "net_pnl": net_pnl,
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": _detect_source(trade),
        "settlement_type": "exit",
        "exit_reason": trade.get("exit_reason"),
        "hold_duration_s": trade.get("hold_duration_s"),
    }
