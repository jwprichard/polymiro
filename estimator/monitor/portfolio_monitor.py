"""portfolio_monitor.py — Portfolio monitoring for open Polymarket positions.

Reads open positions from data/dry_trades.json, fetches current YES prices,
computes edges against predicted probabilities, and emits recommendations
based on the configured risk profile.

Public API
----------
run_monitor(risk_profile=None) -> dict
MonitorError                     exception class

CLI
---
    python -m monitor.portfolio_monitor [--profile conservative|moderate|aggressive]

Exits 0 on success, 1 on unrecoverable I/O error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common import config
from common.io import write_json_atomic
from estimator.scanner.polymarket_client import PolymarketClient, PolymarketClientError

# ---------------------------------------------------------------------------
# Module-level logger — writes to stderr only (no stdout pollution)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit thresholds — defined here, never in config.py
# ---------------------------------------------------------------------------

EXIT_THRESHOLDS: dict[str, float] = {
    "conservative": 0.0,
    "moderate": -0.05,
    "aggressive": -0.10,
}

_VALID_PROFILES = frozenset(EXIT_THRESHOLDS.keys())

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class MonitorError(RuntimeError):
    """Raised for unrecoverable errors in the monitor layer (e.g., bad I/O).

    Per-position fetch failures are recorded in the report and do NOT raise
    this exception.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_monitor(risk_profile: Optional[str] = None) -> dict:
    """Scan all open positions and produce a monitor report.

    Parameters
    ----------
    risk_profile:
        One of "conservative", "moderate", "aggressive".  Defaults to
        ``config.RISK_PROFILE`` when None.

    Returns
    -------
    dict
        The monitor report, structured per the monitor_report.json schema.
        The same dict is written atomically to ``config.MONITOR_REPORT_FILE``.

    Raises
    ------
    MonitorError
        On unrecoverable I/O errors (unreadable dry_trades.json, unwritable
        report path, invalid risk profile).
    """
    # --- Resolve and validate risk profile ---
    profile = risk_profile if risk_profile is not None else config.RISK_PROFILE
    if profile not in _VALID_PROFILES:
        raise MonitorError(
            f"Invalid risk_profile {profile!r}. "
            f"Must be one of: {sorted(_VALID_PROFILES)}"
        )
    threshold = EXIT_THRESHOLDS[profile]

    # --- Load open positions ---
    open_positions = _load_open_positions()
    logger.info("Loaded %d open position(s).", len(open_positions))

    # --- Process each position ---
    client = PolymarketClient()
    position_reports: list[dict] = []

    for idx, trade in enumerate(open_positions):
        if idx > 0:
            time.sleep(config.MONITOR_PRICE_FETCH_DELAY_S)

        report_entry = _process_position(trade, client, threshold)
        position_reports.append(report_entry)

    # --- Summary counts ---
    hold_count = sum(1 for p in position_reports if p["recommendation"] == "HOLD")
    exit_count = sum(1 for p in position_reports if p["recommendation"] == "EXIT")

    report: dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "risk_profile": profile,
        "summary": {
            "total": len(position_reports),
            "hold": hold_count,
            "exit": exit_count,
        },
        "positions": position_reports,
    }

    # --- Write report atomically ---
    try:
        write_json_atomic(config.MONITOR_REPORT_FILE, report)
    except Exception as exc:
        raise MonitorError(
            f"Failed to write monitor report to {config.MONITOR_REPORT_FILE}: {exc}"
        ) from exc

    logger.info(
        "Monitor report written to %s — total=%d hold=%d exit=%d",
        config.MONITOR_REPORT_FILE,
        len(position_reports),
        hold_count,
        exit_count,
    )
    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_open_positions() -> list[dict]:
    """Read dry_trades.json and return entries whose status is not 'exited'.

    Returns an empty list when the file does not exist.

    Raises
    ------
    MonitorError
        If the file exists but cannot be parsed as a JSON array.
    """
    path: Path = config.DRY_TRADES_FILE
    if not path.exists():
        logger.info("No dry_trades.json found at %s; treating as empty.", path)
        return []

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MonitorError(
            f"Cannot parse dry_trades.json at {path}: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise MonitorError(
            f"Expected a JSON array in {path}, got {type(data).__name__}"
        )

    open_positions = [
        entry for entry in data
        if isinstance(entry, dict) and entry.get("status") != "exited"
    ]
    skipped = len(data) - len(open_positions)
    if skipped:
        logger.info("Skipped %d exited position(s).", skipped)
    return open_positions


def _fetch_yes_price(
    trade: dict, client: PolymarketClient
) -> tuple[Optional[float], Optional[str]]:
    """Return (yes_price, fetch_error) for *trade*.

    Fetches the active markets list via ``fetch_active_markets`` and locates
    the position's market by ``market_id``.  Returns the ``yes_price`` field
    from the matching ``Market`` record.

    ``fetch_spread`` is intentionally not used here because it returns the
    bid/ask spread (a width), not the current YES price level.

    On any failure returns (None, error_message).
    """
    market_id: str = trade.get("market_id", "")

    try:
        markets = client.fetch_active_markets(limit=500)
    except PolymarketClientError as exc:
        return None, str(exc)

    for market in markets:
        if market.market_id == market_id:
            return market.yes_price, None

    # Market not found in the active list — may be resolved or closed.
    return None, f"Market {market_id!r} not found in active markets list (limit=500)"


def _load_predicted_probability(market_id: str) -> Optional[float]:
    """Read predicted_probability from data/results/{market_id}.json.

    Returns None and logs a warning if the result file is absent or malformed.
    """
    result_path: Path = config.RESULTS_DIR / f"{market_id}.json"
    if not result_path.exists():
        logger.warning(
            "Result file missing for market %s (expected %s). "
            "Setting current_edge=null and recommendation=HOLD.",
            market_id,
            result_path,
        )
        return None

    try:
        text = result_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not read result file %s: %s. "
            "Setting current_edge=null and recommendation=HOLD.",
            result_path,
            exc,
        )
        return None

    prob = data.get("predicted_probability")
    if prob is None:
        logger.warning(
            "Result file %s has no 'predicted_probability' field. "
            "Setting current_edge=null and recommendation=HOLD.",
            result_path,
        )
        return None

    try:
        return float(prob)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Result file %s has non-numeric predicted_probability %r: %s. "
            "Setting current_edge=null and recommendation=HOLD.",
            result_path,
            prob,
            exc,
        )
        return None


def _compute_edge(
    predicted_probability: Optional[float],
    current_yes_price: Optional[float],
) -> Optional[float]:
    """Return predicted_probability - current_yes_price, or None if either is None."""
    if predicted_probability is None or current_yes_price is None:
        return None
    return predicted_probability - current_yes_price


def _make_recommendation(
    current_edge: Optional[float], threshold: float
) -> str:
    """Return 'EXIT' if current_edge < threshold, else 'HOLD'.

    Returns 'HOLD' when current_edge is None (null-safe).
    """
    if current_edge is None:
        return "HOLD"
    return "EXIT" if current_edge < threshold else "HOLD"


def _process_position(
    trade: dict, client: PolymarketClient, threshold: float
) -> dict:
    """Build the report entry dict for a single open position."""
    market_id: str = trade.get("market_id", "")
    question: str = trade.get("question", "")
    direction: str = trade.get("direction", "YES")

    # Use predicted_probability from trade record as the stored baseline;
    # also load from result file for the current cycle.
    stored_predicted_prob: Optional[float] = trade.get("predicted_probability")
    try:
        stored_predicted_prob = float(stored_predicted_prob) if stored_predicted_prob is not None else None
    except (ValueError, TypeError):
        stored_predicted_prob = None

    # Load fresh predicted_probability from result file (authoritative source)
    predicted_probability: Optional[float] = _load_predicted_probability(market_id)

    # If result file is missing, fall back to the value stored in the trade record
    if predicted_probability is None and stored_predicted_prob is not None:
        predicted_probability = stored_predicted_prob

    # Fetch current YES price
    current_yes_price, fetch_error = _fetch_yes_price(trade, client)
    if fetch_error:
        logger.warning(
            "Price fetch failed for market %s: %s", market_id, fetch_error
        )

    # Compute edge and recommendation
    current_edge = _compute_edge(predicted_probability, current_yes_price)
    recommendation = _make_recommendation(current_edge, threshold)

    # entry_edge: the original edge at the time of trade approval
    entry_edge = trade.get("edge_at_approval")
    try:
        entry_edge = float(entry_edge) if entry_edge is not None else None
    except (ValueError, TypeError):
        entry_edge = None

    return {
        "market_id": market_id,
        "question": question,
        "direction": direction,
        "predicted_probability": predicted_probability,
        "current_yes_price": current_yes_price,
        "current_edge": current_edge,
        "entry_edge": entry_edge,
        "recommendation": recommendation,
        "fetch_error": fetch_error,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m monitor.portfolio_monitor",
        description="Scan open positions and emit a monitor report.",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(_VALID_PROFILES),
        default=None,
        help=(
            "Risk profile to use for exit thresholds. "
            "Overrides the RISK_PROFILE environment variable for this run. "
            f"Choices: {sorted(_VALID_PROFILES)}. "
            f"Default: value of RISK_PROFILE env var (currently {config.RISK_PROFILE!r})."
        ),
    )
    return parser.parse_args(argv)


def _configure_logging() -> None:
    """Route all log output to stderr at INFO level."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns 0 on success, 1 on unrecoverable error."""
    _configure_logging()
    args = _parse_args(argv)

    try:
        report = run_monitor(risk_profile=args.profile)
    except MonitorError as exc:
        logger.error("Monitor run failed: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error during monitor run: %s", exc, exc_info=True)
        return 1

    # Print a human-readable summary to stdout for quick inspection.
    summary = report.get("summary", {})
    print(
        f"Monitor complete — profile={report['risk_profile']} "
        f"total={summary.get('total', 0)} "
        f"hold={summary.get('hold', 0)} "
        f"exit={summary.get('exit', 0)}",
        flush=True,
    )
    print(f"Report written to: {config.MONITOR_REPORT_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
