"""Tests for updown/replay.py -- ReplayEngine, load(), run(), summary(), helpers."""

from __future__ import annotations

import gzip
import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from updown.replay import (
    ReplayEngine,
    TradeEvent,
    _Position,
    _tick_to_context,
    _trades_to_ticks,
    _event_to_dict,
)
from updown.types import MarketState, TickContext

# Import conftest factories (pytest discovers them automatically, but explicit
# import makes the helpers usable as plain functions too).
from updown.tests.conftest import make_strategy_config


# ═══════════════════════════════════════════════════════════════════════════
# Tick / trade data factories
# ═══════════════════════════════════════════════════════════════════════════

# Signal math recap (from signal.py):
#   pct_change = (current - open) / open
#   implied_prob = clamp(0.5 + pct_change / SCALE_FACTOR, 0.01, 0.99)
#   SCALE_FACTOR default = 0.01
#   yes_edge = implied_prob - market_yes_price
#   should_trade = abs(edge) > threshold  AND  abs(pct_change) >= MIN_BTC_PCT_CHANGE
#
# For a strong YES signal: BTC up significantly, yes_price low.
# Example: open=67000, current=67100 => pct_change ~0.00149
#   implied_prob = 0.5 + 0.00149/0.01 = 0.649
#   yes_edge = 0.649 - 0.40 = 0.249 (well above threshold 0.05)


def _make_tick(
    *,
    timestamp_ms: int = 1_700_000_000_000,
    price: float = 67_100.0,
    open_price: float = 67_000.0,
    yes_price: float = 0.40,
    no_price: float = 0.60,
    market_id: str = "0xtest_market",
    token_id: str = "tok_abc",
    expiry_time: float = 1_700_000_300.0,
    price_age_ms: int = 100,
    question: str = "Will BTC go up?",
) -> dict[str, Any]:
    """Build a single tick record dict."""
    return {
        "timestamp_ms": timestamp_ms,
        "price": price,
        "open_price": open_price,
        "yes_price": yes_price,
        "no_price": no_price,
        "market_id": market_id,
        "token_id": token_id,
        "expiry_time": expiry_time,
        "price_age_ms": price_age_ms,
        "question": question,
    }


def _make_entry_tick(ts_ms: int = 1_700_000_000_000, **kwargs: Any) -> dict[str, Any]:
    """Tick that triggers a YES entry (BTC up, yes_price low)."""
    defaults = dict(
        timestamp_ms=ts_ms,
        price=67_100.0,       # BTC went up from open
        open_price=67_000.0,
        yes_price=0.40,       # market underpriced => positive edge
        no_price=0.60,
    )
    defaults.update(kwargs)
    return _make_tick(**defaults)


def _make_hold_tick(ts_ms: int = 1_700_000_005_000, **kwargs: Any) -> dict[str, Any]:
    """Tick where price hasn't moved enough to trigger exit."""
    defaults = dict(
        timestamp_ms=ts_ms,
        price=67_100.0,
        open_price=67_000.0,
        yes_price=0.42,  # small move, within stop_loss/take_profit thresholds
        no_price=0.58,
    )
    defaults.update(kwargs)
    return _make_tick(**defaults)


def _make_take_profit_tick(
    ts_ms: int = 1_700_000_010_000, **kwargs: Any
) -> dict[str, Any]:
    """Tick that triggers take-profit exit (yes_price rose above entry + target_delta)."""
    # Default take_profit delta = 0.06.  If entry was at yes_price=0.40,
    # a yes_price of 0.47 gives profit = 0.47 - 0.40 = 0.07 >= 0.06.
    defaults = dict(
        timestamp_ms=ts_ms,
        price=67_100.0,
        open_price=67_000.0,
        yes_price=0.47,
        no_price=0.53,
    )
    defaults.update(kwargs)
    return _make_tick(**defaults)


def _make_stop_loss_tick(
    ts_ms: int = 1_700_000_010_000, **kwargs: Any
) -> dict[str, Any]:
    """Tick that triggers stop-loss exit (yes_price dropped below entry - max_loss_delta)."""
    # Default stop_loss delta = 0.04.  If entry was at yes_price=0.40,
    # a yes_price of 0.35 gives loss = 0.40 - 0.35 = 0.05 >= 0.04.
    defaults = dict(
        timestamp_ms=ts_ms,
        price=67_100.0,
        open_price=67_000.0,
        yes_price=0.35,
        no_price=0.65,
    )
    defaults.update(kwargs)
    return _make_tick(**defaults)


def _make_time_exit_tick(
    ts_ms: int = 1_700_000_300_000, **kwargs: Any
) -> dict[str, Any]:
    """Tick that triggers time-exit (held > max_hold_seconds=240s)."""
    # Entry at ts=1_700_000_000 => 300s later => 300 >= 240 max_hold.
    defaults = dict(
        timestamp_ms=ts_ms,
        price=67_100.0,
        open_price=67_000.0,
        yes_price=0.41,
        no_price=0.59,
    )
    defaults.update(kwargs)
    return _make_tick(**defaults)


def _make_trade_record(
    *,
    trade_id: str = "t001",
    market_id: str = "0xtest_market",
    asset_id: str = "tok_abc",
    market_price: float = 0.55,
    timestamp_utc: str = "2026-03-29T12:00:00+00:00",
) -> dict[str, Any]:
    """Build a trade-file record (updown_trades.json format)."""
    return {
        "trade_id": trade_id,
        "market_id": market_id,
        "asset_id": asset_id,
        "market_price": market_price,
        "timestamp_utc": timestamp_utc,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _tick_to_context helper
# ═══════════════════════════════════════════════════════════════════════════


class TestTickToContext:
    """Tests for the _tick_to_context() helper."""

    def test_basic_idle_context(self) -> None:
        tick = _make_tick()
        pos = _Position()
        cfg = make_strategy_config()
        ctx = _tick_to_context(tick, pos, cfg)

        assert isinstance(ctx, TickContext)
        assert ctx.tick_price == 67_100.0
        assert ctx.open_price == 67_000.0
        assert ctx.yes_price == 0.40
        assert ctx.no_price == 0.60
        assert ctx.market_id == "0xtest_market"
        assert ctx.state == MarketState.IDLE
        assert ctx.entry_price is None
        assert ctx.entry_time is None
        assert ctx.strategy_config is cfg

    def test_entered_position_state(self) -> None:
        tick = _make_tick()
        pos = _Position(
            state=MarketState.ENTERED,
            entry_price=0.40,
            entry_time=1_700_000_000.0,
            entry_side="YES",
            entry_size_usdc=5.0,
        )
        cfg = make_strategy_config()
        ctx = _tick_to_context(tick, pos, cfg)

        assert ctx.state == MarketState.ENTERED
        assert ctx.entry_price == 0.40
        assert ctx.entry_time == 1_700_000_000.0
        assert ctx.entry_side == "YES"
        assert ctx.entry_size_usdc == 5.0

    def test_defaults_for_missing_keys(self) -> None:
        """Tick dict with missing keys should fall back to defaults."""
        tick: dict[str, Any] = {"market_id": "0xfoo"}
        pos = _Position()
        cfg = make_strategy_config()
        ctx = _tick_to_context(tick, pos, cfg)

        assert ctx.tick_price == 0.0
        assert ctx.open_price == 0.0
        assert ctx.yes_price == 0.5
        assert ctx.no_price == 0.5
        assert ctx.price_age_ms == 0
        assert ctx.market_id == "0xfoo"
        assert ctx.question == ""
        assert ctx.token_id == ""
        assert ctx.expiry_time == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _trades_to_ticks conversion
# ═══════════════════════════════════════════════════════════════════════════


class TestTradesToTicks:
    """Tests for the _trades_to_ticks() helper."""

    def test_basic_conversion(self) -> None:
        trades = [
            _make_trade_record(market_price=0.55),
            _make_trade_record(trade_id="t002", market_price=0.70),
        ]
        ticks = _trades_to_ticks(trades)

        assert len(ticks) == 2
        assert ticks[0]["yes_price"] == 0.55
        assert ticks[0]["no_price"] == pytest.approx(0.45)
        assert ticks[0]["market_id"] == "0xtest_market"
        assert ticks[0]["token_id"] == "tok_abc"
        # BTC price and open_price not available in trade files
        assert ticks[0]["price"] == 0.0
        assert ticks[0]["open_price"] == 0.0

        assert ticks[1]["yes_price"] == 0.70
        assert ticks[1]["no_price"] == pytest.approx(0.30)

    def test_timestamp_parsing(self) -> None:
        trades = [_make_trade_record(timestamp_utc="2026-03-29T12:00:00+00:00")]
        ticks = _trades_to_ticks(trades)
        # Should parse to a nonzero timestamp
        assert ticks[0]["timestamp_ms"] > 0

    def test_invalid_timestamp_defaults_to_zero(self) -> None:
        trades = [_make_trade_record(timestamp_utc="not-a-date")]
        ticks = _trades_to_ticks(trades)
        assert ticks[0]["timestamp_ms"] == 0

    def test_empty_timestamp(self) -> None:
        trades = [_make_trade_record(timestamp_utc="")]
        ticks = _trades_to_ticks(trades)
        assert ticks[0]["timestamp_ms"] == 0

    def test_no_price_complement(self) -> None:
        """no_price should be 1.0 - market_price."""
        trades = [_make_trade_record(market_price=0.80)]
        ticks = _trades_to_ticks(trades)
        assert ticks[0]["no_price"] == pytest.approx(0.20)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine.load()
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayLoad:
    """Tests for ReplayEngine.load() file handling."""

    def test_load_jsonl(self, tmp_path: Path) -> None:
        """Load a JSONL file (one JSON object per line)."""
        tick1 = _make_tick(timestamp_ms=1000)
        tick2 = _make_tick(timestamp_ms=2000)
        p = tmp_path / "ticks.jsonl"
        p.write_text(json.dumps(tick1) + "\n" + json.dumps(tick2) + "\n")

        cfg = make_strategy_config()
        engine = ReplayEngine.load(p, strategy_config=cfg)
        assert len(engine._ticks) == 2
        assert engine._source_type == "tick"

    def test_load_json_array(self, tmp_path: Path) -> None:
        """Load a JSON array file."""
        ticks = [_make_tick(timestamp_ms=1000), _make_tick(timestamp_ms=2000)]
        p = tmp_path / "ticks.json"
        p.write_text(json.dumps(ticks))

        cfg = make_strategy_config()
        engine = ReplayEngine.load(p, strategy_config=cfg)
        assert len(engine._ticks) == 2
        assert engine._source_type == "tick"

    def test_load_gzipped_jsonl(self, tmp_path: Path) -> None:
        """Load a gzipped JSONL file."""
        tick = _make_tick()
        p = tmp_path / "ticks.jsonl.gz"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(json.dumps(tick) + "\n")

        cfg = make_strategy_config()
        engine = ReplayEngine.load(p, strategy_config=cfg)
        assert len(engine._ticks) == 1

    def test_load_gzipped_json_array(self, tmp_path: Path) -> None:
        """Load a gzipped JSON array file."""
        ticks = [_make_tick(timestamp_ms=1000)]
        p = tmp_path / "ticks.json.gz"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(json.dumps(ticks))

        cfg = make_strategy_config()
        engine = ReplayEngine.load(p, strategy_config=cfg)
        assert len(engine._ticks) == 1

    def test_load_trade_file(self, tmp_path: Path) -> None:
        """Loading a trade file auto-detects trade format and converts."""
        trades = [_make_trade_record(), _make_trade_record(trade_id="t002")]
        p = tmp_path / "trades.json"
        p.write_text(json.dumps(trades))

        cfg = make_strategy_config()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            engine = ReplayEngine.load(p, strategy_config=cfg)
            assert len(w) == 1
            assert "lower fidelity" in str(w[0].message)

        assert engine._source_type == "trade"
        assert len(engine._ticks) == 2

    def test_load_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            ReplayEngine.load("/nonexistent/path.jsonl", strategy_config=make_strategy_config())

    def test_load_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        with pytest.raises(ValueError, match="File is empty"):
            ReplayEngine.load(p, strategy_config=make_strategy_config())

    def test_load_whitespace_only_file(self, tmp_path: Path) -> None:
        p = tmp_path / "ws.jsonl"
        p.write_text("   \n  \n  ")
        with pytest.raises(ValueError, match="File is empty"):
            ReplayEngine.load(p, strategy_config=make_strategy_config())

    def test_load_unrecognised_format(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.txt"
        p.write_text("not json at all")
        with pytest.raises(ValueError, match="Unrecognised file format"):
            ReplayEngine.load(p, strategy_config=make_strategy_config())

    def test_load_invalid_jsonl_line(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.jsonl"
        p.write_text('{"ok": 1}\n{broken\n')
        with pytest.raises(ValueError, match="Invalid JSON on line 2"):
            ReplayEngine.load(p, strategy_config=make_strategy_config())

    def test_load_json_array_not_list(self, tmp_path: Path) -> None:
        """JSON that starts with '[' but top-level is wrong type after parse (edge case)."""
        # json.loads of "[1,2,3]" returns a list, but e.g. a file starting with
        # '[' that decodes to a non-list shouldn't happen in practice; tested
        # for defensive coverage.
        # Actually, anything starting with '[' will parse to a list in valid JSON.
        # So test an empty array instead.
        p = tmp_path / "empty_array.json"
        p.write_text("[]")
        with pytest.raises(ValueError, match="No records found"):
            ReplayEngine.load(p, strategy_config=make_strategy_config())

    def test_load_jsonl_skips_blank_lines(self, tmp_path: Path) -> None:
        tick = _make_tick()
        p = tmp_path / "ticks.jsonl"
        p.write_text("\n" + json.dumps(tick) + "\n\n" + json.dumps(tick) + "\n\n")

        # File starts with newline, but after lstrip the first char is '{'.
        # Actually, lstrip removes leading whitespace, so the blank lines before
        # the first record are stripped. Blank lines between records are skipped
        # by the parser.
        cfg = make_strategy_config()
        engine = ReplayEngine.load(p, strategy_config=cfg)
        assert len(engine._ticks) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine.run() -- entry triggered
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayRunEntry:
    """Tests that run() correctly triggers entry signals."""

    def test_entry_triggered(self) -> None:
        """A single strong-signal tick should produce an entry event."""
        ticks = [_make_entry_tick()]
        cfg = make_strategy_config(edge_threshold=0.05)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        entries = [e for e in events if e.kind == "entry"]
        assert len(entries) == 1
        assert entries[0].side == "YES"
        assert entries[0].market_id == "0xtest_market"
        assert entries[0].price == pytest.approx(0.40)  # filled at yes_price
        assert entries[0].edge > 0.05

    def test_no_entry_when_below_threshold(self) -> None:
        """A tick where BTC barely moved should not trigger entry."""
        tick = _make_tick(
            price=67_001.0,     # tiny move
            open_price=67_000.0,
            yes_price=0.50,
            no_price=0.50,
        )
        cfg = make_strategy_config(edge_threshold=0.05)
        engine = ReplayEngine([tick], strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()
        assert len(events) == 0

    def test_no_entry_when_open_price_zero(self) -> None:
        """Tick with open_price=0 should skip entry evaluation."""
        tick = _make_entry_tick(open_price=0.0)
        cfg = make_strategy_config()
        engine = ReplayEngine([tick], strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()
        assert len(events) == 0

    def test_multiple_markets_independent(self) -> None:
        """Different market_ids maintain separate position state."""
        tick_a = _make_entry_tick(market_id="0xmarket_a")
        tick_b = _make_entry_tick(market_id="0xmarket_b", ts_ms=1_700_000_001_000)

        cfg = make_strategy_config()
        engine = ReplayEngine([tick_a, tick_b], strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        entries = [e for e in events if e.kind == "entry"]
        assert len(entries) == 2
        assert {e.market_id for e in entries} == {"0xmarket_a", "0xmarket_b"}


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine.run() -- exit triggered
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayRunExit:
    """Tests that run() correctly triggers exit signals."""

    def test_take_profit_exit(self) -> None:
        """Entry followed by a take-profit tick should produce entry + exit."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        entries = [e for e in events if e.kind == "entry"]
        exits = [e for e in events if e.kind == "exit"]
        assert len(entries) == 1
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"
        assert exits[0].entry_price == pytest.approx(0.40)
        # PnL = (current_price - entry_price) * size
        # = (0.47 - 0.40) * 5.0 = 0.35
        assert exits[0].pnl == pytest.approx(0.35)
        assert exits[0].hold_duration_s == pytest.approx(10.0)

    def test_stop_loss_exit(self) -> None:
        """Entry followed by a stop-loss tick should produce a stop_loss exit."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_stop_loss_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(stop_loss_delta=0.04)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        exits = [e for e in events if e.kind == "exit"]
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"
        # PnL = (0.35 - 0.40) * 5.0 = -0.25
        assert exits[0].pnl == pytest.approx(-0.25)

    def test_time_exit(self) -> None:
        """Entry followed by a tick far in the future triggers time exit."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_time_exit_tick(ts_ms=1_700_000_300_000),
        ]
        cfg = make_strategy_config(max_hold_seconds=240.0)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        exits = [e for e in events if e.kind == "exit"]
        assert len(exits) == 1
        assert exits[0].reason == "time_exit"
        # hold_duration = 300s
        assert exits[0].hold_duration_s == pytest.approx(300.0)

    def test_no_entry_on_exit_tick(self) -> None:
        """The tick that triggers an exit should NOT also evaluate entry (continue)."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            # This tick triggers exit AND would trigger entry if not for `continue`
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06, allow_reentry=True)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        # Should have exactly 1 entry + 1 exit, no second entry on the exit tick
        entries = [e for e in events if e.kind == "entry"]
        exits = [e for e in events if e.kind == "exit"]
        assert len(entries) == 1
        assert len(exits) == 1

    def test_reentry_after_exit_with_allow_reentry(self) -> None:
        """With allow_reentry=True, a new entry can happen after exit."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
            # Third tick: triggers re-entry (state reset to IDLE by allow_reentry)
            _make_entry_tick(ts_ms=1_700_000_020_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06, allow_reentry=True)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        entries = [e for e in events if e.kind == "entry"]
        assert len(entries) == 2

    def test_no_reentry_without_allow_reentry(self) -> None:
        """With allow_reentry=False, state goes to COOLDOWN so no re-entry."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
            _make_entry_tick(ts_ms=1_700_000_020_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06, allow_reentry=False)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        entries = [e for e in events if e.kind == "entry"]
        assert len(entries) == 1  # Only the first entry


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine.run() -- correct PnL calculation
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayPnL:
    """Tests for PnL correctness in run()."""

    def test_positive_pnl_on_take_profit(self) -> None:
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        exit_event = [e for e in events if e.kind == "exit"][0]
        # entry at yes_price=0.40, exit at yes_price=0.47, size=5.0
        expected_pnl = (0.47 - 0.40) * 5.0
        assert exit_event.pnl == pytest.approx(expected_pnl)

    def test_negative_pnl_on_stop_loss(self) -> None:
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_stop_loss_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(stop_loss_delta=0.04)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        events = engine.run()

        exit_event = [e for e in events if e.kind == "exit"][0]
        # entry at 0.40, exit at 0.35
        expected_pnl = (0.35 - 0.40) * 5.0
        assert exit_event.pnl == pytest.approx(expected_pnl)
        assert exit_event.pnl < 0

    def test_custom_trade_amount(self) -> None:
        """trade_amount_usdc is used for PnL calculation."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06)
        engine = ReplayEngine(
            ticks, strategy_config=cfg, edge_threshold=0.05, trade_amount_usdc=10.0
        )
        events = engine.run()

        exit_event = [e for e in events if e.kind == "exit"][0]
        expected_pnl = (0.47 - 0.40) * 10.0
        assert exit_event.pnl == pytest.approx(expected_pnl)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine.summary()
# ═══════════════════════════════════════════════════════════════════════════


class TestReplaySummary:
    """Tests for the summary() aggregate statistics."""

    def test_summary_before_run_raises(self) -> None:
        cfg = make_strategy_config()
        engine = ReplayEngine([], strategy_config=cfg)
        with pytest.raises(RuntimeError, match="Call run\\(\\) before summary"):
            engine.summary()

    def test_summary_empty_run(self) -> None:
        """Running with no ticks produces zeros."""
        cfg = make_strategy_config()
        engine = ReplayEngine([], strategy_config=cfg)
        engine.run()
        s = engine.summary()

        assert s["total_ticks"] == 0
        assert s["signals_generated"] == 0
        assert s["trades_entered"] == 0
        assert s["trades_exited"] == 0
        assert s["wins"] == 0
        assert s["losses"] == 0
        assert s["total_pnl"] == 0.0
        assert s["max_drawdown"] == 0.0

    def test_summary_wins_and_losses(self) -> None:
        """Verify correct win/loss counts and total_pnl."""
        # Trade 1: entry + take profit (win)
        # Trade 2: entry + stop loss (loss) -- requires allow_reentry
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
            _make_entry_tick(ts_ms=1_700_000_020_000),
            _make_stop_loss_tick(ts_ms=1_700_000_030_000),
        ]
        cfg = make_strategy_config(
            take_profit_delta=0.06,
            stop_loss_delta=0.04,
            allow_reentry=True,
        )
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()

        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["trades_entered"] == 2
        assert s["trades_exited"] == 2
        # total_pnl = +0.35 + (-0.25) = +0.10
        assert s["total_pnl"] == pytest.approx(0.10)

    def test_summary_max_drawdown(self) -> None:
        """max_drawdown tracks peak-to-trough in cumulative PnL."""
        # Craft a scenario: win, then loss, then loss
        # Cumulative: +0.35, +0.35-0.25=+0.10
        # Peak = 0.35, trough after = 0.10, drawdown = 0.25
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
            _make_entry_tick(ts_ms=1_700_000_020_000),
            _make_stop_loss_tick(ts_ms=1_700_000_030_000),
        ]
        cfg = make_strategy_config(
            take_profit_delta=0.06,
            stop_loss_delta=0.04,
            allow_reentry=True,
        )
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()

        # Cumulative PnL: [+0.35, +0.10]
        # Peak after trade1 = 0.35, after trade2 cumulative=0.10
        # Drawdown = 0.35 - 0.10 = 0.25
        assert s["max_drawdown"] == pytest.approx(0.25)

    def test_summary_total_ticks(self) -> None:
        ticks = [_make_tick() for _ in range(5)]
        cfg = make_strategy_config()
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()
        assert s["total_ticks"] == 5

    def test_summary_source_type(self) -> None:
        cfg = make_strategy_config()
        engine = ReplayEngine([], strategy_config=cfg, source_type="trade")
        engine.run()
        s = engine.summary()
        assert s["source_type"] == "trade"

    def test_summary_events_serialized(self) -> None:
        """summary() events are plain dicts, not TradeEvent objects."""
        ticks = [_make_entry_tick()]
        cfg = make_strategy_config()
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()

        assert len(s["events"]) > 0
        assert isinstance(s["events"][0], dict)
        assert "kind" in s["events"][0]

    def test_summary_signals_generated(self) -> None:
        """signals_generated counts how many entries were triggered."""
        ticks = [_make_entry_tick()]
        cfg = make_strategy_config()
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()
        assert s["signals_generated"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _event_to_dict serialization
# ═══════════════════════════════════════════════════════════════════════════


class TestEventToDict:
    """Tests for the _event_to_dict() helper."""

    def test_entry_event_serialization(self) -> None:
        event = TradeEvent(
            kind="entry",
            tick_index=0,
            timestamp_ms=1000,
            market_id="0xabc",
            side="YES",
            price=0.50,
            size_usdc=5.0,
            edge=0.08,
            reason="test",
        )
        d = _event_to_dict(event)
        assert d["kind"] == "entry"
        assert d["edge"] == pytest.approx(0.08)
        assert "pnl" not in d  # entry events don't have pnl
        assert "entry_price" not in d

    def test_exit_event_serialization(self) -> None:
        event = TradeEvent(
            kind="exit",
            tick_index=5,
            timestamp_ms=2000,
            market_id="0xabc",
            side="YES",
            price=0.55,
            size_usdc=5.0,
            edge=0.0,
            reason="take_profit",
            entry_price=0.50,
            pnl=0.25,
            hold_duration_s=10.5,
        )
        d = _event_to_dict(event)
        assert d["kind"] == "exit"
        assert d["entry_price"] == 0.50
        assert d["pnl"] == pytest.approx(0.25)
        assert d["hold_duration_s"] == pytest.approx(10.5)

    def test_exit_event_none_pnl(self) -> None:
        """pnl=None should serialize as 0.0."""
        event = TradeEvent(
            kind="exit",
            tick_index=1,
            timestamp_ms=1000,
            market_id="0x",
            side="YES",
            price=0.50,
            size_usdc=5.0,
            edge=0.0,
            reason="test",
            entry_price=0.50,
            pnl=None,
            hold_duration_s=None,
        )
        d = _event_to_dict(event)
        assert d["pnl"] == 0.0
        assert d["hold_duration_s"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Tests: ReplayEngine constructor and config injection
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayEngineConstructor:
    """Tests for constructor behavior and config injection."""

    def test_strategy_config_injected(self) -> None:
        """Strategy config passed to constructor is used (no filesystem load)."""
        cfg = make_strategy_config(edge_threshold=0.10)
        engine = ReplayEngine([], strategy_config=cfg, edge_threshold=0.10)
        assert engine._strategy is cfg
        assert engine._edge_threshold == 0.10

    def test_edge_threshold_override(self) -> None:
        cfg = make_strategy_config()
        engine = ReplayEngine([], strategy_config=cfg, edge_threshold=0.20)
        assert engine._edge_threshold == 0.20

    def test_trade_amount_override(self) -> None:
        cfg = make_strategy_config()
        engine = ReplayEngine([], strategy_config=cfg, trade_amount_usdc=25.0)
        assert engine._trade_amount == 25.0

    def test_run_can_be_called_twice(self) -> None:
        """Calling run() twice resets events."""
        ticks = [_make_entry_tick()]
        cfg = make_strategy_config()
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)

        events1 = engine.run()
        events2 = engine.run()
        assert len(events1) == len(events2)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: End-to-end replay scenario
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayEndToEnd:
    """Full replay scenario from load to summary."""

    def test_full_cycle_from_jsonl(self, tmp_path: Path) -> None:
        """Load JSONL, run, get summary with correct stats."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_hold_tick(ts_ms=1_700_000_005_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        p = tmp_path / "ticks.jsonl"
        p.write_text("\n".join(json.dumps(t) for t in ticks) + "\n")

        cfg = make_strategy_config(take_profit_delta=0.06)
        engine = ReplayEngine.load(p, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()

        assert s["total_ticks"] == 3
        assert s["trades_entered"] == 1
        assert s["trades_exited"] == 1
        assert s["wins"] == 1
        assert s["losses"] == 0
        assert s["total_pnl"] > 0
        assert s["max_drawdown"] == 0.0

    def test_summary_json_serializable(self, tmp_path: Path) -> None:
        """summary() output must be JSON serializable."""
        ticks = [
            _make_entry_tick(ts_ms=1_700_000_000_000),
            _make_take_profit_tick(ts_ms=1_700_000_010_000),
        ]
        cfg = make_strategy_config(take_profit_delta=0.06)
        engine = ReplayEngine(ticks, strategy_config=cfg, edge_threshold=0.05)
        engine.run()
        s = engine.summary()

        # Should not raise
        serialized = json.dumps(s)
        assert isinstance(serialized, str)
