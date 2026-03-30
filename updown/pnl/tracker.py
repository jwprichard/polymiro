"""P&L tracker — settles dry-mode trades against Gamma API resolution data.

Reads trade records from data/dry_trades.json and data/updown_trades.json,
checks each market's resolution status via the Gamma API, computes P&L for
resolved trades, and writes the updated report to data/pnl_report.json.

Designed to be run repeatedly (e.g. via cron or a loop).  Unresolved markets
are skipped and retried on the next invocation.  Already-settled trades are
deduplicated by trade_id so the report is append-only.

Usage:
    python -m pnl.tracker
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from common import config
from common.log import ulog
from updown.pnl.gamma_client import check_resolution
from updown.pnl.calculator import calculate_pnl, calculate_exit_pnl
from common.io import write_json_atomic

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

_DRY_TRADES_FILE: Path = config.UPDOWN_DATA_DIR / "dry_trades.json"
_UPDOWN_TRADES_FILE: Path = config.UPDOWN_DATA_DIR / "updown_trades.json"
_PNL_REPORT_FILE: Path = config.PNL_REPORT_FILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_list(path: Path) -> list[dict]:
    """Load a JSON array from *path*, returning [] if the file is missing or empty."""
    if not path.exists():
        ulog.pnl.info("Trade file not found, treating as empty: %s", path)
        return []
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            ulog.pnl.warning("Expected JSON array in %s, got %s — skipping", path, type(data).__name__)
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        ulog.pnl.error("Failed to read %s: %s", path, exc)
        return []


def _normalise_trade(trade: dict) -> dict | None:
    """Return a copy of *trade* with a consistent ``outcome_bet`` field.

    Research trades carry ``direction`` ("YES"/"NO").
    Updown trades carry ``outcome`` ("yes"/"no").

    Both are mapped to ``outcome_bet`` in uppercase ("YES"/"NO").
    The original trade dict is not mutated.

    Returns None if no bet-direction field can be found.
    """
    # Already has outcome_bet — nothing to do.
    if trade.get("outcome_bet"):
        return trade

    normalised = dict(trade)

    # Research trades: direction field.
    direction = trade.get("direction")
    if direction and direction.upper() in ("YES", "NO"):
        normalised["outcome_bet"] = direction.upper()
        return normalised

    # Updown trades: outcome field (lowercase "yes"/"no").
    outcome = trade.get("outcome")
    if outcome and outcome.upper() in ("YES", "NO"):
        normalised["outcome_bet"] = outcome.upper()
        return normalised

    ulog.pnl.warning(
        "Cannot determine bet direction for trade_id=%s — skipping",
        trade.get("trade_id", "<unknown>"),
    )
    return None


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset() -> None:
    """Overwrite pnl_report.json and updown_trades.json with empty lists.

    Uses :func:`write_json_atomic` for safe, atomic writes.
    dry_trades.json is intentionally left untouched.
    """
    for path in (_PNL_REPORT_FILE, _UPDOWN_TRADES_FILE):
        write_json_atomic(path, [])
    print(f"Reset: cleared {_PNL_REPORT_FILE} and {_UPDOWN_TRADES_FILE}")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def run() -> None:
    """Execute one pass of the P&L tracker.

    1. Load trades from both source files.
    2. Filter to dry_mode: true only.
    3. Load existing report and build a set of already-resolved trade_ids.
    4. Batch Gamma API calls by unique condition_id (market_id).
    5. For resolved markets, calculate P&L and append to the report.
    6. Write updated report atomically.
    7. Log a summary.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    # --- 1. Load trades ------------------------------------------------------
    dry_trades = _load_json_list(_DRY_TRADES_FILE)
    updown_trades = _load_json_list(_UPDOWN_TRADES_FILE)
    all_trades = dry_trades + updown_trades

    # --- 2. Filter to dry-mode only ------------------------------------------
    dry_only = [t for t in all_trades if t.get("dry_mode") is True]
    ulog.pnl.info(
        "Loaded %d total trades (%d dry_trades, %d updown_trades), %d are dry_mode",
        len(all_trades),
        len(dry_trades),
        len(updown_trades),
        len(dry_only),
    )

    if not dry_only:
        ulog.pnl.info("No dry-mode trades to process — exiting.")
        return

    # --- 3. Load existing report and build dedup set -------------------------
    existing_report: list[dict] = _load_json_list(_PNL_REPORT_FILE)
    resolved_ids: set[str] = {r["trade_id"] for r in existing_report if "trade_id" in r}
    ulog.pnl.info(
        "Existing report contains %d resolved trades",
        len(resolved_ids),
    )

    # Filter out already-resolved trades.
    pending = [t for t in dry_only if t.get("trade_id") not in resolved_ids]
    if not pending:
        _log_summary(total_checked=len(dry_only), newly_resolved=0, report=existing_report)
        return

    # --- 4. Normalise trades -------------------------------------------------
    normalised: list[dict] = []
    for trade in pending:
        norm = _normalise_trade(trade)
        if norm is not None:
            normalised.append(norm)

    # --- 5. Partition: exit trades vs open positions -------------------------
    exit_trades = [
        t for t in normalised
        if t.get("direction") == "sell" and t.get("exit_price") is not None
    ]
    settled_keys: set[tuple[str, str]] = {
        (t["market_id"], t.get("asset_id", ""))
        for t in exit_trades
    }
    gamma_candidates = [
        t for t in normalised
        if t.get("direction") != "sell"
        and (t.get("market_id"), t.get("asset_id", "")) not in settled_keys
    ]
    ulog.pnl.info(
        "Partitioned %d normalised trades: %d exit, %d gamma candidates, %d matched buys skipped",
        len(normalised), len(exit_trades), len(gamma_candidates),
        len(normalised) - len(exit_trades) - len(gamma_candidates),
    )

    # --- 6a. Settle exit trades from entry/exit spread -----------------------
    new_results: list[dict] = []

    for trade in exit_trades:
        try:
            pnl_record = calculate_exit_pnl(trade)
            new_results.append(pnl_record)
            ulog.pnl.debug(
                "Exit P&L for %s: net=%.6f (%s)",
                trade["trade_id"], pnl_record["net_pnl"], trade.get("exit_reason"),
            )
        except (KeyError, ValueError) as exc:
            ulog.pnl.error(
                "Failed to calculate exit P&L for trade %s: %s",
                trade.get("trade_id"), exc,
            )

    # --- 6b. Batch Gamma API calls for remaining open positions --------------
    market_ids: set[str] = {t["market_id"] for t in gamma_candidates if "market_id" in t}
    resolution_cache: dict[str, dict | None] = {}

    for market_id in market_ids:
        resolution_cache[market_id] = check_resolution(market_id)

    unresolved_count = 0

    for trade in gamma_candidates:
        market_id = trade.get("market_id")
        if not market_id:
            ulog.pnl.warning("Trade %s has no market_id — skipping", trade.get("trade_id"))
            continue

        resolution = resolution_cache.get(market_id)
        if resolution is None:
            ulog.pnl.info(
                "Gamma API returned no data for market_id=%s (trade %s) — will retry",
                market_id, trade["trade_id"],
            )
            unresolved_count += 1
            continue

        if not resolution.get("resolved"):
            ulog.pnl.info(
                "Market %s not yet resolved — skipping trade %s",
                market_id, trade["trade_id"],
            )
            unresolved_count += 1
            continue

        winning_outcome = resolution["outcome"]
        try:
            pnl_record = calculate_pnl(trade, winning_outcome)
            new_results.append(pnl_record)
        except (KeyError, ValueError) as exc:
            ulog.pnl.error(
                "Failed to calculate P&L for trade %s: %s",
                trade.get("trade_id"), exc,
            )

    # --- 7. Write updated report ---------------------------------------------
    updated_report = existing_report + new_results
    ulog.pnl.debug(
        "Writing pnl_report.json: %d existing + %d new = %d total records -> %s",
        len(existing_report), len(new_results), len(updated_report), _PNL_REPORT_FILE,
    )
    write_json_atomic(_PNL_REPORT_FILE, updated_report)
    ulog.pnl.info("Wrote %d new P&L records to %s", len(new_results), _PNL_REPORT_FILE)

    # --- 8. Summary ----------------------------------------------------------
    _log_summary(
        total_checked=len(dry_only),
        newly_resolved=len(new_results),
        report=updated_report,
    )


def _log_summary(*, total_checked: int, newly_resolved: int, report: list[dict]) -> None:
    """Log a human-readable summary of this tracker run."""
    cumulative_pnl = sum(r.get("net_pnl", 0.0) for r in report)
    ulog.pnl.info(
        "P&L tracker summary: %d trades checked, %d newly resolved, "
        "cumulative net P&L: %.6f USDC (%d total settled)",
        total_checked,
        newly_resolved,
        cumulative_pnl,
        len(report),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
    sys.exit(0)
