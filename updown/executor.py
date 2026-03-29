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
from typing import Optional

from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import BUY, SELL

import config
from updown.types import OrderResult, TradeIntent
from utils.io import atomic_append_to_json_list

logger = logging.getLogger(__name__)

# Polygon mainnet chain ID — required by py-clob-client for signing.
_POLYGON_CHAIN_ID = 137

# Default HTTP timeout for order submission (seconds).
_REQUEST_TIMEOUT_S = 10


class ExecutorError(Exception):
    """Raised when order placement fails in a recoverable way."""


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


async def place_order(
    intent: TradeIntent,
    *,
    edge: float = 0.0,
    implied_prob: float = 0.0,
    market_price: float = 0.0,
    timeout_s: float = _REQUEST_TIMEOUT_S,
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
        _persist_trade(trade_id, intent, result, edge, implied_prob, market_price, dry=True)
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
        trade_id, intent, result, edge, implied_prob, market_price, dry=False
    )
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_trade(
    trade_id: str,
    intent: TradeIntent,
    result: OrderResult,
    edge: float,
    implied_prob: float,
    market_price: float,
    *,
    dry: bool,
) -> None:
    """Append a complete trade record to ``UPDOWN_TRADES_FILE``."""
    record = {
        "trade_id": trade_id,
        "asset_id": intent.token_id,
        "direction": intent.side,
        "edge": round(edge, 6),
        "implied_prob": round(implied_prob, 6),
        "market_price": round(market_price, 6),
        "amount_usdc": intent.size_usdc,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "dry" if dry else ("filled" if result.success else "failed"),
        "dry_mode": dry,
        "order_id": result.order_id,
        "outcome": intent.outcome,
        "market_id": intent.market_id,
    }
    try:
        atomic_append_to_json_list(config.UPDOWN_TRADES_FILE, record)
    except Exception:
        # Persistence failure must not crash the trading loop.
        logger.exception("Failed to persist trade record %s", trade_id)
