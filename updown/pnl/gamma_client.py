"""
Gamma API resolution checker for Polymarket markets.

Queries the Gamma REST API to determine whether a market has resolved and,
if so, which outcome won.  Used by the P&L tracker to settle dry-mode trades.

Gamma API contract (validated 2026-03-29):
    GET {GAMMA_API_BASE_URL}/markets?condition_ids={condition_id}
    Returns a JSON list of market records.  A resolved market has:
        closed: true
        acceptingOrders: false
        outcomePrices: e.g. ["1", "0"]   -- winning outcome index has price "1"
        outcomes:      e.g. ["Yes", "No"]
"""

import json
from typing import Optional

import requests

from common.config import GAMMA_API_BASE_URL
from common.log import ulog

_TIMEOUT_S = 15  # Consistent with existing Gamma calls in updown/loop.py


class GammaClientError(RuntimeError):
    """Raised on Gamma API failures.  Never escapes check_resolution()."""


def check_resolution(condition_id: str) -> Optional[dict]:
    """Query the Gamma API for market resolution status.

    Args:
        condition_id: Hex condition ID (e.g. ``"0x482a83..."``) that
            identifies the market on Polymarket.

    Returns:
        A dict ``{"resolved": bool, "outcome": "Yes"|"No"|None}`` when the
        API responds successfully:

        - ``resolved=True, outcome="Yes"`` -- first outcome won
        - ``resolved=True, outcome="No"``  -- second outcome won
        - ``resolved=False, outcome=None`` -- market still open

        Returns ``None`` if the API call fails (HTTP error, timeout,
        malformed response, or market not found).  All errors are logged;
        no exception escapes this function.
    """
    try:
        return _query_gamma(condition_id)
    except Exception:
        ulog.gamma.exception(
            "Unexpected error checking resolution for condition_id=%s",
            condition_id,
        )
        return None


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _query_gamma(condition_id: str) -> Optional[dict]:
    """Perform the actual HTTP request and parse the response."""
    url = f"{GAMMA_API_BASE_URL.rstrip('/')}/markets"

    try:
        resp = requests.get(
            url,
            params={"condition_ids": condition_id},
            timeout=_TIMEOUT_S,
        )
    except requests.ConnectionError:
        ulog.gamma.error(
            "Gamma API connection error for condition_id=%s", condition_id
        )
        return None
    except requests.Timeout:
        ulog.gamma.error(
            "Gamma API timed out after %ds for condition_id=%s",
            _TIMEOUT_S,
            condition_id,
        )
        return None
    except requests.RequestException as exc:
        ulog.gamma.error(
            "Gamma API request failed for condition_id=%s: %s",
            condition_id,
            exc,
        )
        return None

    if resp.status_code != 200:
        ulog.gamma.error(
            "Gamma API returned HTTP %d for condition_id=%s: %s",
            resp.status_code,
            condition_id,
            resp.text[:300],
        )
        return None

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        ulog.gamma.error(
            "Gamma API returned non-JSON body for condition_id=%s: %s",
            condition_id,
            resp.text[:200],
        )
        return None

    # The Gamma list endpoint returns a JSON array.
    records = data if isinstance(data, list) else [data]
    if not records or not isinstance(records[0], dict):
        ulog.gamma.warning(
            "Gamma API returned no market for condition_id=%s", condition_id
        )
        return None

    # Find the record matching our condition_id (case-insensitive hex match).
    market = _find_market(records, condition_id)
    if market is None:
        ulog.gamma.warning(
            "Gamma API response did not contain condition_id=%s "
            "(got %d records)",
            condition_id,
            len(records),
        )
        return None

    return _parse_resolution(market, condition_id)


def _find_market(records: list[dict], condition_id: str) -> Optional[dict]:
    """Return the record whose conditionId matches, or None."""
    target = condition_id.lower()
    for record in records:
        cid = record.get("conditionId") or record.get("condition_id", "")
        if cid.lower() == target:
            return record
    # If only one record was returned, trust the API filter.
    if len(records) == 1:
        return records[0]
    return None


def _parse_resolution(market: dict, condition_id: str) -> dict:
    """Extract resolution status from a single Gamma market record.

    Resolution logic:
        A market is considered resolved when ``closed`` is True and
        ``acceptingOrders`` is False and exactly one entry in
        ``outcomePrices`` equals ``"1"`` (the winner).

    The outcome is normalised to ``"Yes"`` or ``"No"``:
        - Index 0 winning  ->  ``"Yes"``   (first outcome won)
        - Index 1 winning  ->  ``"No"``    (second outcome won)

    This normalisation works for standard Yes/No markets.  For non-standard
    outcomes (Up/Down, team names, Over/Under) the same positional mapping
    applies: first outcome is treated as the "Yes" equivalent.
    """
    closed = bool(market.get("closed", False))
    accepting_orders = bool(market.get("acceptingOrders", True))

    if not closed or accepting_orders:
        return {"resolved": False, "outcome": None}

    # Parse outcomePrices -- may be a JSON-encoded string or a real list.
    raw_prices = market.get("outcomePrices", [])
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except (ValueError, json.JSONDecodeError):
            raw_prices = []

    if not isinstance(raw_prices, list) or len(raw_prices) < 2:
        ulog.gamma.warning(
            "Cannot determine resolution for condition_id=%s: "
            "outcomePrices=%r",
            condition_id,
            raw_prices,
        )
        return {"resolved": False, "outcome": None}

    # Determine winning index: the outcome whose price is "1" (or closest).
    try:
        prices = [float(p) for p in raw_prices[:2]]
    except (ValueError, TypeError):
        ulog.gamma.warning(
            "Non-numeric outcomePrices for condition_id=%s: %r",
            condition_id,
            raw_prices,
        )
        return {"resolved": False, "outcome": None}

    # A cleanly resolved market has one price at 1.0 and the other at 0.0.
    # Guard against mid-resolution states where prices haven't fully settled.
    if prices[0] == 1.0 and prices[1] == 0.0:
        winning_index = 0
    elif prices[1] == 1.0 and prices[0] == 0.0:
        winning_index = 1
    else:
        # Prices have not fully settled to 0/1 -- treat as unresolved.
        ulog.gamma.info(
            "Market condition_id=%s is closed but prices not settled: %r",
            condition_id,
            prices,
        )
        return {"resolved": False, "outcome": None}

    # Normalise: index 0 -> "Yes", index 1 -> "No".
    outcome = "Yes" if winning_index == 0 else "No"

    return {"resolved": True, "outcome": outcome}
