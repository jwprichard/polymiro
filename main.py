"""main.py — CLI entry point for the Polymarket × MiroFish pipeline.

Usage
-----
    python main.py --version
    python main.py scan [--log DEBUG]
    python main.py research [--log DEBUG]
    python main.py select [--log DEBUG]
    python main.py review [--dry-run] [--log DEBUG]
    python main.py monitor [--profile conservative|moderate|aggressive] [--log DEBUG]
    python main.py updown [--dry-run] [--edge-threshold 0.05] [--log DEBUG]
    python main.py backtest --file <path> [--strategy strategy.yml] [--edge-threshold 0.05] [--output results.json] [--log DEBUG]
    python main.py pnl [--reset] [--log DEBUG]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subcommand: scan
# ---------------------------------------------------------------------------


def cmd_scan(_args: argparse.Namespace) -> int:
    """Execute one Polymarket scan cycle."""
    from estimator.scanner.scanner_agent import run_scan
    from estimator.scanner.polymarket_client import PolymarketClientError

    try:
        results = run_scan()
        print(json.dumps(results, indent=2, default=str))
        return 0
    except PolymarketClientError as exc:
        logger.error("Scan failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# Subcommand: research
# ---------------------------------------------------------------------------


def cmd_research(_args: argparse.Namespace) -> int:
    """Run the research pipeline for the top unprocessed opportunity."""
    from estimator.research.research_agent import run_research

    try:
        result = run_research()
        if result is None:
            print("No unprocessed opportunities found.")
            return 0
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        logger.error("Research failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# Subcommand: select
# ---------------------------------------------------------------------------


def cmd_select(_args: argparse.Namespace) -> int:
    """Score and rank research results; write pending_trades.json."""
    from estimator.selector.selector_agent import run_selector

    try:
        result = run_selector()
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        logger.error("Selector failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# Subcommand: review
# ---------------------------------------------------------------------------


def cmd_review(args: argparse.Namespace) -> int:
    """Interactively review pending trades and execute approved ones."""
    from common import config

    if args.dry_run:
        config.DRY_MODE = True
        logger.info("--dry-run flag set: DRY_MODE forced to True for this session.")

    data_dir = Path(config.ESTIMATOR_DATA_DIR)
    pending_path = data_dir / "pending_trades.json"

    if not pending_path.exists():
        print("No pending trades found. Run 'select' first.")
        return 0

    try:
        with open(pending_path) as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read pending_trades.json: %s", exc)
        return 1

    if not trades:
        print("No pending trades.")
        return 0

    from estimator.trading.trade_executor import execute_trades

    try:
        results = execute_trades(trades)
        print(json.dumps(results, indent=2, default=str))
        return 0
    except Exception as exc:
        logger.error("Trade execution failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# Subcommand: monitor
# ---------------------------------------------------------------------------


def cmd_monitor(args: argparse.Namespace) -> int:
    """Check open positions and print hold/exit recommendations."""
    from common import config

    if args.profile is not None:
        config.RISK_PROFILE = args.profile
        logger.info("Risk profile overridden to '%s'.", config.RISK_PROFILE)

    from estimator.monitor.monitor_agent import run_monitor

    try:
        result = run_monitor()
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        logger.error("Monitor failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# Subcommand: updown
# ---------------------------------------------------------------------------


def cmd_updown(args: argparse.Namespace) -> int:
    """Launch the async updown orchestrator loop."""
    import asyncio

    from common import config
    from updown.strategy_config import load_strategy_config

    # Load and validate strategy config (fail fast on bad YAML).
    strategy_path = Path(args.strategy)
    try:
        strategy_cfg = load_strategy_config(strategy_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    logger.info("Loaded strategy config from %s", strategy_path)

    # --dry-run flag: force UPDOWN_DRY_MODE before any updown imports.
    if args.dry_run:
        config.UPDOWN_DRY_MODE = True
        logger.info("--dry-run flag set: UPDOWN_DRY_MODE forced to True for this session.")

    # --edge-threshold override.
    if args.edge_threshold is not None:
        config.UPDOWN_EDGE_THRESHOLD = args.edge_threshold
        logger.info("Edge threshold overridden to %.4f.", config.UPDOWN_EDGE_THRESHOLD)

    # --no-tick-log: disable tick JSONL recording.
    if args.no_tick_log:
        config.UPDOWN_TICK_LOG_ENABLED = False

    # Fail fast if live mode but missing API credentials.
    if not config.UPDOWN_DRY_MODE:
        missing: list[str] = []
        for attr in ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"):
            if not getattr(config, attr, None):
                missing.append(attr)
        if missing:
            print(
                f"ERROR: live mode requires config values: {', '.join(missing)}. "
                "Set them or pass --dry-run.",
                file=sys.stderr,
            )
            return 1

    from updown.loop import run

    try:
        asyncio.run(run(strategy_config=strategy_cfg))
    except KeyboardInterrupt:
        logger.info("Updown interrupted by user.")
    except Exception as exc:
        logger.error("Updown crashed: %s", exc)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand: backtest
# ---------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay tick logs through the pure decision pipeline."""
    from datetime import datetime, timezone
    from updown.replay import ReplayEngine
    from updown.strategy_config import load_strategy_config

    # Load strategy config if provided.
    strategy_cfg = None
    if args.strategy:
        try:
            strategy_cfg = load_strategy_config(Path(args.strategy))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        logger.info("Loaded strategy config from %s", args.strategy)

    try:
        engine = ReplayEngine.load(
            path=args.file,
            strategy_config=strategy_cfg,
            edge_threshold=args.edge_threshold,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    engine.run()
    result = engine.summary()

    output_text = json.dumps(result, indent=2)
    print(output_text)

    # Default to data/backtests/backtest_YYYYMMDD_HHMMSS.json when no
    # explicit --output is given, so results accumulate automatically.
    if args.output:
        out_path = Path(args.output)
    else:
        from common import config
        backtests_dir = config.UPDOWN_DATA_DIR / "backtests"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        source_stem = Path(args.file).stem
        out_path = backtests_dir / f"backtest_{source_stem}_{stamp}.json"

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text + "\n", encoding="utf-8")
        logger.info("Results written to %s", out_path)
        print(f"\nSaved to {out_path}")
    except OSError as exc:
        print(f"ERROR writing output file: {exc}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Subcommand: pnl
# ---------------------------------------------------------------------------


def cmd_pnl(args: argparse.Namespace) -> int:
    """Settle dry-mode trades and compute cumulative P&L."""
    if args.reset:
        from updown.pnl.tracker import reset
        reset()
        return 0

    from updown.pnl.tracker import run

    try:
        run()
        return 0
    except Exception as exc:
        logger.error("P&L tracker failed: %s", exc)
        return 1


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main",
        description="Polymarket x MiroFish pipeline CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Shared parent so --log works on every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log",
        metavar="LEVEL",
        default="INFO",
        help="Set log level (DEBUG, INFO, WARNING, ERROR). Default: INFO",
    )
    common.add_argument(
        "--filter",
        metavar="CATEGORIES",
        default=None,
        help=(
            "Comma-separated log category filter. "
            "e.g. --filter poly,signal,exit  or  --filter '*,-binance'. "
            "Categories: poly, binance, signal, entry, exit, gamma, rest, "
            "position, rotate, backpressure, latency, dry, live, pnl, "
            "executor, retry, startup."
        ),
    )

    subparsers = parser.add_subparsers(dest="subcommand", title="subcommands")

    # --- scan ---
    subparsers.add_parser(
        "scan", parents=[common],
        help="Execute one Polymarket scan cycle and write opportunities.json.",
    )

    # --- research ---
    subparsers.add_parser(
        "research", parents=[common],
        help="Run the research pipeline for the top unprocessed opportunity.",
    )

    # --- select ---
    subparsers.add_parser(
        "select", parents=[common],
        help="Score and rank research results; write pending_trades.json.",
    )

    # --- review ---
    review_parser = subparsers.add_parser(
        "review", parents=[common],
        help="Interactively review pending trades and execute approved ones.",
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force DRY_MODE=True for this session (no live orders submitted).",
    )

    # --- monitor ---
    monitor_parser = subparsers.add_parser(
        "monitor", parents=[common],
        help="Check open positions and print hold/exit recommendations.",
    )
    monitor_parser.add_argument(
        "--profile",
        choices=["conservative", "moderate", "aggressive"],
        default=None,
        help="Override RISK_PROFILE for this session.",
    )

    # --- updown ---
    updown_parser = subparsers.add_parser(
        "updown", parents=[common],
        help="Launch the real-time updown orchestrator loop.",
    )
    updown_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force UPDOWN_DRY_MODE=True for this session (no live orders).",
    )
    updown_parser.add_argument(
        "--edge-threshold",
        type=float,
        default=None,
        help="Override UPDOWN_EDGE_THRESHOLD for this session.",
    )
    updown_parser.add_argument(
        "--strategy",
        type=str,
        default="updown/strategy.yml",
        help="Path to strategy YAML config file (default: updown/strategy.yml).",
    )
    updown_parser.add_argument(
        "--no-tick-log",
        action="store_true",
        default=False,
        help="Disable tick-level JSONL logging (enabled by default).",
    )

    # --- backtest ---
    backtest_parser = subparsers.add_parser(
        "backtest", parents=[common],
        help="Replay tick logs through the updown decision pipeline.",
    )
    backtest_parser.add_argument(
        "--file",
        type=str,
        required=True,
        help="Path to JSONL tick log or JSON array file to replay.",
    )
    backtest_parser.add_argument(
        "--strategy",
        type=str,
        default="updown/strategy.yml",
        help="Path to strategy YAML config file (default: updown/strategy.yml).",
    )
    backtest_parser.add_argument(
        "--edge-threshold",
        type=float,
        default=None,
        help="Override UPDOWN_EDGE_THRESHOLD for this replay.",
    )
    backtest_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write results JSON to this file path.",
    )

    # --- pnl ---
    pnl_parser = subparsers.add_parser(
        "pnl", parents=[common],
        help="Settle dry-mode trades against Gamma API and compute P&L.",
    )
    pnl_parser.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="Delete pnl_report.json and start fresh.",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(0)

    level = getattr(logging, args.log.upper(), None)
    if not isinstance(level, int):
        print(f"Invalid log level: {args.log}", file=sys.stderr)
        sys.exit(1)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Apply --filter before any updown/estimator imports so categories
    # are configured before CategoryLogger instances are created.
    if getattr(args, "filter", None):
        from common.log import apply_filter
        apply_filter(args.filter)

    dispatch = {
        "scan": cmd_scan,
        "research": cmd_research,
        "select": cmd_select,
        "review": cmd_review,
        "monitor": cmd_monitor,
        "updown": cmd_updown,
        "backtest": cmd_backtest,
        "pnl": cmd_pnl,
    }

    handler = dispatch.get(args.subcommand)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    main()
