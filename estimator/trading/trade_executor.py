"""Trade execution layer.

Handles interactive candidate review and order submission.

DRY_MODE (default True) logs trade records to data/dry_trades.json without
ever submitting an order to Polymarket.  Set DRY_MODE=false in the
environment only when live trading is intended.

Public API
----------
present_for_review(candidates)  -> list[dict]
execute_trade(candidate)         -> dict
TradeExecutionError              exception class
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

import config
from utils.io import atomic_append_to_json_list


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class TradeExecutionError(Exception):
    """Raised for unrecoverable configuration errors in the trading layer.

    This is NOT raised for per-trade subprocess failures; those are captured
    in the trade record as status='failed' and still appended to the file.
    """


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------


def present_for_review(candidates: list[dict]) -> list[dict]:
    """Present ranked candidates interactively and return approved trade records.

    For each candidate a formatted card is printed.  The user is prompted:
        [y]es / [n]o / [s]kip / [q]uit

    EOF (Ctrl-D) and non-tty stdin are treated as skip-all so the function
    is safe to call in automated pipelines.

    Parameters
    ----------
    candidates:
        Ordered list of opportunity dicts produced by the ranking layer.

    Returns
    -------
    list[dict]
        Trade records (status='pending') for every approved candidate.
    """
    approved: list[dict] = []

    # Detect non-interactive context once before the loop.
    interactive = sys.stdin.isatty()

    for rank, candidate in enumerate(candidates, start=1):
        _print_candidate_card(rank, candidate)

        if not interactive:
            # Non-tty pipe: treat as skip-all, no prompt.
            print("  [non-tty input — skipping]", flush=True)
            continue

        answer = _prompt_user()

        if answer == "q":
            print("  Quitting review.", flush=True)
            break
        elif answer == "y":
            record = _build_trade_record(candidate)
            approved.append(record)
            print(f"  Approved trade_id={record['trade_id']}", flush=True)
        elif answer == "n":
            print("  Rejected.", flush=True)
        else:
            # "s" or anything else
            print("  Skipped.", flush=True)

    return approved


def _print_candidate_card(rank: int, candidate: dict) -> None:
    """Print a formatted summary card for one candidate."""
    print(flush=True)
    print(f"--- Candidate #{rank} ---", flush=True)
    print(f"  Question       : {candidate.get('question', 'N/A')}", flush=True)
    print(f"  Direction      : {candidate.get('direction', 'N/A')}", flush=True)
    print(f"  Current YES px : {candidate.get('current_yes_price', 'N/A')}", flush=True)
    print(f"  Edge           : {candidate.get('edge', 'N/A')}", flush=True)
    print(f"  Confidence     : {candidate.get('confidence', 'N/A')}", flush=True)
    print(f"  Composite score: {candidate.get('composite_score', 'N/A')}", flush=True)


def _prompt_user() -> str:
    """Prompt the user and return a normalised single-character response.

    Returns one of: 'y', 'n', 's', 'q'.
    EOF (Ctrl-D) returns 's' (skip).
    """
    valid = {"y", "n", "s", "q"}
    while True:
        try:
            print("  Approve trade? [y]es / [n]o / [s]kip / [q]uit: ", end="", flush=True)
            raw = input().strip().lower()
        except EOFError:
            print(flush=True)
            return "s"

        if raw in valid:
            return raw
        if raw:
            print(f"  Unknown input '{raw}'. Please enter y, n, s, or q.", flush=True)


def _build_trade_record(candidate: dict) -> dict:
    """Build a pending trade record from a candidate dict."""
    return {
        "trade_id": str(uuid.uuid4()),
        "market_id": candidate.get("market_id", ""),
        "question": candidate.get("question", ""),
        "direction": candidate.get("direction", "YES"),
        "amount_usdc": config.TRADE_AMOUNT_USDC,
        "edge_at_approval": candidate.get("edge"),
        "confidence_at_approval": candidate.get("confidence"),
        "composite_score_at_approval": candidate.get("composite_score"),
        "predicted_probability": candidate.get("predicted_probability"),
        "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_mode": config.DRY_MODE,
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_trade(candidate: dict) -> dict:
    """Execute a single approved trade record.

    Parameters
    ----------
    candidate:
        A trade record dict, typically one produced by ``present_for_review``
        (status='pending').  Plain candidate dicts (from the ranking layer)
        are also accepted; ``_build_trade_record`` is called internally to
        normalise them.

    Returns
    -------
    dict
        The finalised trade record with status set to 'executed' or 'failed'.

    Raises
    ------
    TradeExecutionError
        Only for unrecoverable configuration problems (e.g., the
        ``polymarket-cli`` binary is not found when DRY_MODE=False).
        Per-trade subprocess failures do NOT raise; they set
        ``status='failed'`` and are still appended to the file.
    """
    # Normalise: if the caller passed a raw candidate dict (no trade_id),
    # build a proper record first.
    if "trade_id" not in candidate:
        record = _build_trade_record(candidate)
    else:
        record = dict(candidate)

    if config.DRY_MODE:
        record["status"] = "executed"
        atomic_append_to_json_list(config.DRY_TRADES_FILE, record)
        return record

    # Live execution path.
    market_id: str = record.get("market_id", "")
    amount: float = record.get("amount_usdc", config.TRADE_AMOUNT_USDC)

    cli_cmd = [
        config.POLYMARKET_CLI_BIN,
        "buy",
        market_id,
        str(amount),
        "--output", "json",
    ]

    try:
        result = subprocess.run(
            cli_cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TradeExecutionError(
            f"polymarket-cli binary not found: '{config.POLYMARKET_CLI_BIN}'. "
            "Set POLYMARKET_CLI_BIN env var or ensure the binary is on PATH."
        ) from exc

    if result.returncode == 0:
        record["status"] = "executed"
    else:
        record["status"] = "failed"
        record["cli_error"] = (result.stderr or result.stdout or "").strip()

    atomic_append_to_json_list(config.DRY_TRADES_FILE, record)
    return record
