"""
scanner_agent.py — Orchestrator for one Polymarket scan cycle.

Responsibilities:
1. Fetch active markets via PolymarketClient.
2. Exclude markets whose close date is in the past.
3. Sort remaining markets by volume_24h descending; fetch spreads for the
   top SPREAD_FETCH_LIMIT markets (spread = 0.0 for the rest).
4. Score each market; filter by min_score.
5. Write results atomically to DATA_DIR/opportunities.json.
6. Return the list of Opportunity objects.

No side effects at import time — all logic lives inside functions.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import config
from scanner.models import Market, Opportunity
from scanner.polymarket_client import PolymarketClient, PolymarketClientError
from scanner.opportunity_scorer import score_opportunity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_closes_at(closes_at: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 close-date string into a timezone-aware datetime.

    Handles both 'Z' suffix and '+00:00' offset.  Returns None if the string
    is None, empty, or cannot be parsed (caller treats None as "keep the
    market").
    """
    if not closes_at:
        return None

    closes_str = closes_at.strip()

    # Normalize 'Z' → '+00:00' for Python < 3.11 fromisoformat compatibility.
    if closes_str.endswith("Z"):
        closes_str = closes_str[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(closes_str)
    except ValueError:
        logger.debug(
            "Could not parse closes_at %r — market will not be excluded.", closes_at
        )
        return None

    # Attach UTC if the datetime is naïve.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _is_resolved(market: Market, now: datetime) -> bool:
    """Return True when the market's close date is strictly in the past."""
    dt = _parse_closes_at(market.closes_at)
    if dt is None:
        return False
    return dt < now


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_scan(
    limit: int = config.SCANNER_MARKET_LIMIT,
    min_score: float = config.SCANNER_MIN_SCORE,
) -> list[Opportunity]:
    """Execute one full scan cycle and return qualifying Opportunity objects.

    Parameters
    ----------
    limit:
        Maximum number of markets to fetch from Polymarket.
    min_score:
        Minimum opportunity_score (inclusive) for a market to be included in
        the output.

    Returns
    -------
    list[Opportunity]
        Sorted descending by opportunity_score.  May be empty.
    """
    # Step 1: consistent timestamp for the entire run.
    scanned_at: str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Step 2: fetch markets.
    client = PolymarketClient()
    markets: list[Market] = client.fetch_active_markets(limit)
    logger.info("Markets fetched: %d", len(markets))

    # Step 3: exclude already-resolved markets.
    now = datetime.now(tz=timezone.utc)
    active_markets: list[Market] = []
    excluded_count = 0
    for market in markets:
        if _is_resolved(market, now):
            excluded_count += 1
        else:
            active_markets.append(market)
    logger.info("Markets excluded as resolved (past close date): %d", excluded_count)

    # Step 4: sort by volume_24h descending; select top N for spread fetching.
    active_markets.sort(key=lambda m: m.volume_24h, reverse=True)
    spread_fetch_set = active_markets[: config.SPREAD_FETCH_LIMIT]
    beyond_spread_set = active_markets[config.SPREAD_FETCH_LIMIT :]

    # Step 5: fetch spreads for top-N; use 0.0 for the rest.
    spread_map: dict[str, float] = {}

    for market in spread_fetch_set:
        try:
            spread_map[market.market_id] = client.fetch_spread(market.token_id)
        except PolymarketClientError as exc:
            logger.warning(
                "Could not fetch spread for market %s (%r): %s — using 0.0",
                market.market_id,
                market.question[:60],
                exc,
            )
            spread_map[market.market_id] = 0.0

    for market in beyond_spread_set:
        spread_map[market.market_id] = 0.0

    # Step 6 & 7: score each market and filter by min_score.
    logger.info("Markets scored: %d", len(active_markets))

    opportunities: list[Opportunity] = []
    for market in active_markets:
        spread_value = spread_map[market.market_id]
        score, sources = score_opportunity(market, spread_value)

        if score >= min_score:
            opportunities.append(
                Opportunity(
                    market_id=market.market_id,
                    question=market.question,
                    current_yes_price=market.yes_price,
                    current_no_price=market.no_price,
                    volume_24h=market.volume_24h,
                    spread=spread_value,
                    closes_at=market.closes_at,
                    opportunity_score=score,
                    data_sources_suggested=sources,
                    scanned_at=scanned_at,
                )
            )

    logger.info(
        "Markets above score threshold (%.2f): %d", min_score, len(opportunities)
    )

    # Step 8: sort descending by opportunity_score.
    opportunities.sort(key=lambda o: o.opportunity_score, reverse=True)

    # Steps 10 & 11: atomic write to DATA_DIR/opportunities.json.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    tmp_path = config.DATA_DIR / "opportunities.tmp.json"
    out_path = config.DATA_DIR / "opportunities.json"

    serializable = [dataclasses.asdict(opp) for opp in opportunities]

    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(serializable, fh, indent=2)
        fh.write("\n")  # POSIX-friendly trailing newline

    os.replace(tmp_path, out_path)

    logger.info("Opportunities written to %s", out_path)

    return opportunities


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    try:
        results = run_scan()
    except PolymarketClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Scan complete: {len(results)} opportunities written to"
        f" data/opportunities.json"
    )
    sys.exit(0)
