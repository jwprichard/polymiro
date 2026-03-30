"""updown/replay.py -- Synchronous replay harness for tick logs and trade files.

Replays pre-recorded tick data through the pure decision functions in
``updown/decisions.py`` without any asyncio or network calls.  Produces
aggregate statistics suitable for backtesting strategy changes.

Usage::

    engine = ReplayEngine.load("data/updown_ticks_2026-03-29.jsonl")
    engine.run()
    print(json.dumps(engine.summary(), indent=2))
"""

from __future__ import annotations

import json
import logging
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from updown.decisions import evaluate_entry, evaluate_exit
from updown.exit_rules import ExitSignal
from updown.strategy_config import StrategyConfig, load_strategy_config
from updown.types import MarketState, TickContext, TradeIntent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade event container
# ---------------------------------------------------------------------------


@dataclass
class TradeEvent:
    """A single entry or exit recorded during replay."""

    kind: str  # "entry" | "exit"
    tick_index: int
    timestamp_ms: int
    market_id: str
    side: str  # "YES" | "NO"
    price: float
    size_usdc: float
    edge: float
    reason: str
    # Exit-specific fields
    entry_price: Optional[float] = None
    pnl: Optional[float] = None
    hold_duration_s: Optional[float] = None


# ---------------------------------------------------------------------------
# Simulated position state
# ---------------------------------------------------------------------------


@dataclass
class _Position:
    """Mutable position state maintained during replay."""

    state: MarketState = MarketState.IDLE
    entry_price: Optional[float] = None
    entry_time: Optional[float] = None
    entry_side: Optional[str] = None
    entry_size_usdc: Optional[float] = None


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Synchronous replay harness that drives pure decision functions.

    Loads tick data (JSONL or JSON array) or trade files, replays them
    through ``evaluate_entry`` and ``evaluate_exit``, and collects
    aggregate statistics.

    Parameters
    ----------
    ticks:
        List of tick dicts as produced by ``TickLogger._tick_to_record``.
    strategy_config:
        Strategy configuration.  If ``None``, loads ``strategy.yml``
        from the current directory.
    edge_threshold:
        Overrides ``config.UPDOWN_EDGE_THRESHOLD`` for this replay.
    trade_amount_usdc:
        Position size for simulated entries.
    source_type:
        ``"tick"`` for tick logs, ``"trade"`` for trade files.
    """

    def __init__(
        self,
        ticks: list[dict[str, Any]],
        strategy_config: Optional[StrategyConfig] = None,
        edge_threshold: Optional[float] = None,
        trade_amount_usdc: float = 5.0,
        source_type: str = "tick",
    ) -> None:
        self._ticks = ticks
        self._source_type = source_type

        # Load strategy config lazily if not provided.
        if strategy_config is not None:
            self._strategy = strategy_config
        else:
            self._strategy = load_strategy_config(Path("strategy.yml"))

        # Resolve edge threshold.
        if edge_threshold is not None:
            self._edge_threshold = edge_threshold
        else:
            import config
            self._edge_threshold = config.UPDOWN_EDGE_THRESHOLD

        self._trade_amount = trade_amount_usdc

        # Results populated by run().
        self._events: list[TradeEvent] = []
        self._has_run = False

        # Per-run aggregate counters.
        self._total_ticks = 0
        self._signals_generated = 0

    # ------------------------------------------------------------------
    # Factory: load
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: str | Path,
        strategy_config: Optional[StrategyConfig] = None,
        edge_threshold: Optional[float] = None,
        trade_amount_usdc: float = 5.0,
    ) -> ReplayEngine:
        """Load tick data from *path* and return a configured engine.

        Auto-detects format by inspecting the first non-whitespace
        character:

        - ``{`` -- JSONL (one JSON object per line)
        - ``[`` -- JSON array of objects

        Additionally detects ``updown_trades.json`` format (array of
        trade records with ``trade_id`` keys) and emits a fidelity
        warning.
        """
        filepath = Path(path)
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        raw_text = filepath.read_text(encoding="utf-8").lstrip()
        if not raw_text:
            raise ValueError(f"File is empty: {filepath}")

        first_char = raw_text[0]

        if first_char == "[":
            records = json.loads(raw_text)
            if not isinstance(records, list):
                raise ValueError(f"Expected JSON array, got {type(records).__name__}")
        elif first_char == "{":
            # JSONL: one JSON object per line.
            records = []
            for i, line in enumerate(raw_text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON on line {i} of {filepath}: {exc}"
                    ) from exc
        else:
            raise ValueError(
                f"Unrecognised file format in {filepath}: "
                f"expected first character '{{' or '[', got {first_char!r}"
            )

        if not records:
            raise ValueError(f"No records found in {filepath}")

        # Detect trade-file format vs. tick-log format.
        source_type = "tick"
        sample = records[0]
        if "trade_id" in sample:
            source_type = "trade"
            warnings.warn(
                f"Loading trade-file format from {filepath}. "
                "Trade-file replay has lower fidelity: no open_price, "
                "no NO price, no price_age_ms. Results are approximate.",
                stacklevel=2,
            )
            records = _trades_to_ticks(records)

        return cls(
            ticks=records,
            strategy_config=strategy_config,
            edge_threshold=edge_threshold,
            trade_amount_usdc=trade_amount_usdc,
            source_type=source_type,
        )

    # ------------------------------------------------------------------
    # Core: run
    # ------------------------------------------------------------------

    def run(self) -> list[TradeEvent]:
        """Replay all ticks synchronously through the decision pipeline.

        Returns a list of trade events (entries and exits only).
        """
        self._events = []
        self._total_ticks = len(self._ticks)
        self._signals_generated = 0

        # Per-market position state.
        positions: dict[str, _Position] = {}

        for idx, tick in enumerate(self._ticks):
            market_id = tick.get("market_id", "unknown")

            if market_id not in positions:
                positions[market_id] = _Position()
            pos = positions[market_id]

            # Build TickContext from the tick record.
            ctx = _tick_to_context(tick, pos, self._strategy)

            now_s = tick.get("timestamp_ms", 0) / 1000.0
            tick_price = tick.get("price", 0.0)
            open_price = tick.get("open_price", 0.0)

            # --- Exit evaluation (if we hold a position) ---
            if pos.state == MarketState.ENTERED:
                # Determine position-side price.
                if pos.entry_side == "NO":
                    position_price = ctx.no_price
                else:
                    position_price = ctx.yes_price

                exit_signal: Optional[ExitSignal] = evaluate_exit(
                    ctx, position_price, now_s,
                )
                if exit_signal is not None:
                    # Record exit event.
                    assert pos.entry_price is not None
                    assert pos.entry_time is not None
                    pnl = (position_price - pos.entry_price) * (
                        pos.entry_size_usdc or self._trade_amount
                    )
                    hold_s = now_s - pos.entry_time

                    self._events.append(TradeEvent(
                        kind="exit",
                        tick_index=idx,
                        timestamp_ms=tick.get("timestamp_ms", 0),
                        market_id=market_id,
                        side=pos.entry_side or "YES",
                        price=position_price,
                        size_usdc=pos.entry_size_usdc or self._trade_amount,
                        edge=0.0,
                        reason=exit_signal.reason,
                        entry_price=pos.entry_price,
                        pnl=pnl,
                        hold_duration_s=hold_s,
                    ))

                    # Reset position to IDLE (or COOLDOWN based on allow_reentry).
                    if self._strategy.exit_rules.allow_reentry:
                        pos.state = MarketState.IDLE
                    else:
                        pos.state = MarketState.COOLDOWN
                    pos.entry_price = None
                    pos.entry_time = None
                    pos.entry_side = None
                    pos.entry_size_usdc = None
                    continue  # Do not evaluate entry on the same tick.

            # --- Entry evaluation (if idle) ---
            if pos.state == MarketState.IDLE and open_price > 0:
                intent: Optional[TradeIntent] = evaluate_entry(
                    ctx=ctx,
                    btc_current=tick_price,
                    btc_open=open_price,
                    threshold=self._edge_threshold,
                    trade_amount_usdc=self._trade_amount,
                    now=now_s,
                )
                if intent is not None:
                    self._signals_generated += 1

                    # Simulate instant fill.
                    side = intent.signal.direction  # "YES" or "NO"
                    fill_price = (
                        ctx.no_price if side == "NO" else ctx.yes_price
                    )

                    pos.state = MarketState.ENTERED
                    pos.entry_price = fill_price
                    pos.entry_time = now_s
                    pos.entry_side = side
                    pos.entry_size_usdc = intent.size_usdc

                    self._events.append(TradeEvent(
                        kind="entry",
                        tick_index=idx,
                        timestamp_ms=tick.get("timestamp_ms", 0),
                        market_id=market_id,
                        side=side,
                        price=fill_price,
                        size_usdc=intent.size_usdc,
                        edge=intent.signal.edge,
                        reason=intent.reason,
                    ))

        self._has_run = True
        return list(self._events)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return aggregate replay statistics.

        Returns
        -------
        dict with keys:
            total_ticks, signals_generated, trades_entered, trades_exited,
            wins, losses, total_pnl, max_drawdown, events
        """
        if not self._has_run:
            raise RuntimeError("Call run() before summary().")

        entries = [e for e in self._events if e.kind == "entry"]
        exits = [e for e in self._events if e.kind == "exit"]

        wins = sum(1 for e in exits if (e.pnl or 0) > 0)
        losses = sum(1 for e in exits if (e.pnl or 0) <= 0)
        total_pnl = sum(e.pnl or 0 for e in exits)

        # Max drawdown: track cumulative P&L and find the largest peak-to-trough.
        max_drawdown = 0.0
        cumulative = 0.0
        peak = 0.0
        for e in exits:
            cumulative += e.pnl or 0
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_drawdown:
                max_drawdown = dd

        return {
            "total_ticks": self._total_ticks,
            "signals_generated": self._signals_generated,
            "trades_entered": len(entries),
            "trades_exited": len(exits),
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 6),
            "max_drawdown": round(max_drawdown, 6),
            "source_type": self._source_type,
            "edge_threshold": self._edge_threshold,
            "events": [_event_to_dict(e) for e in self._events],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tick_to_context(
    tick: dict[str, Any],
    pos: _Position,
    strategy: StrategyConfig,
) -> TickContext:
    """Build a TickContext from a tick record dict and current position state."""
    return TickContext(
        tick_price=tick.get("price", 0.0),
        tick_timestamp_ms=tick.get("timestamp_ms", 0),
        open_price=tick.get("open_price", 0.0),
        yes_price=tick.get("yes_price", 0.5),
        no_price=tick.get("no_price", 0.5),
        price_age_ms=tick.get("price_age_ms", 0),
        market_id=tick.get("market_id", "unknown"),
        question=tick.get("question", ""),
        token_id=tick.get("token_id", ""),
        expiry_time=tick.get("expiry_time", 0.0),
        state=pos.state,
        entry_price=pos.entry_price,
        entry_time=pos.entry_time,
        entry_side=pos.entry_side,
        entry_size_usdc=pos.entry_size_usdc,
        strategy_config=strategy,
    )


def _trades_to_ticks(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert updown_trades.json records into pseudo-tick records.

    Trade files lack open_price, NO price, and price_age_ms.  We
    synthesize approximate values so the replay engine can process them,
    but results will have lower fidelity than tick-log replay.
    """
    from datetime import datetime, timezone

    ticks: list[dict[str, Any]] = []
    for trade in trades:
        # Parse timestamp.
        ts_str = trade.get("timestamp_utc", "")
        try:
            dt = datetime.fromisoformat(ts_str)
            ts_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            ts_ms = 0

        market_price = trade.get("market_price", 0.5)

        ticks.append({
            "timestamp_ms": ts_ms,
            "price": 0.0,  # BTC price not available in trade files
            "open_price": 0.0,  # Not available
            "yes_price": market_price,
            "no_price": round(1.0 - market_price, 6) if market_price else 0.5,
            "price_age_ms": 0,
            "market_id": trade.get("market_id", "unknown"),
            "token_id": trade.get("asset_id", ""),
            "expiry_time": 0.0,
            "state": "idle",
        })
    return ticks


def _event_to_dict(event: TradeEvent) -> dict[str, Any]:
    """Serialise a TradeEvent to a plain dict for JSON output."""
    d: dict[str, Any] = {
        "kind": event.kind,
        "tick_index": event.tick_index,
        "timestamp_ms": event.timestamp_ms,
        "market_id": event.market_id,
        "side": event.side,
        "price": event.price,
        "size_usdc": event.size_usdc,
        "edge": round(event.edge, 6),
        "reason": event.reason,
    }
    if event.kind == "exit":
        d["entry_price"] = event.entry_price
        d["pnl"] = round(event.pnl, 6) if event.pnl is not None else 0.0
        d["hold_duration_s"] = (
            round(event.hold_duration_s, 3)
            if event.hold_duration_s is not None
            else 0.0
        )
    return d
