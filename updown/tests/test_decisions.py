"""Tests for updown/decisions.py -- pure decision functions.

Covers:
- evaluate_expiry: expired detection, non-expired skip, zero expiry_time skip
- evaluate_exit: delegation to check_exit, state guards, missing entry fields
- evaluate_entry: TradeIntent on signal, state guards, btc_open guard, ValueError handling
"""

from __future__ import annotations

import pytest

from updown.decisions import evaluate_entry, evaluate_exit, evaluate_expiry
from updown.exit_rules import ExitSignal
from updown.types import MarketState, TradeIntent

# Import factories from conftest (available via conftest.py in the tests package).
from updown.tests.conftest import make_strategy_config, make_tick_context


# ═══════════════════════════════════════════════════════════════════════════
# evaluate_expiry
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluateExpiry:
    """Tests for evaluate_expiry()."""

    def test_expired_market_detected(self):
        """A market whose expiry_time is in the past should be returned."""
        ctx = make_tick_context(expiry_time=1000.0)
        tracked = {"cid_a": ctx}
        result = evaluate_expiry(tracked, now=1001.0)
        assert result == ["cid_a"]

    def test_non_expired_market_skipped(self):
        """A market whose expiry_time is in the future should not be returned."""
        ctx = make_tick_context(expiry_time=2000.0)
        tracked = {"cid_a": ctx}
        result = evaluate_expiry(tracked, now=1000.0)
        assert result == []

    def test_exact_expiry_boundary_not_expired(self):
        """When now == expiry_time, the market is NOT expired (strictly less than)."""
        ctx = make_tick_context(expiry_time=1000.0)
        tracked = {"cid_a": ctx}
        result = evaluate_expiry(tracked, now=1000.0)
        assert result == []

    def test_zero_expiry_time_skipped(self):
        """A market with expiry_time=0 (disabled) should never be pruned."""
        ctx = make_tick_context(expiry_time=0.0)
        tracked = {"cid_a": ctx}
        result = evaluate_expiry(tracked, now=999_999_999.0)
        assert result == []

    def test_negative_expiry_time_skipped(self):
        """A market with negative expiry_time should never be pruned."""
        ctx = make_tick_context(expiry_time=-1.0)
        tracked = {"cid_a": ctx}
        result = evaluate_expiry(tracked, now=999_999_999.0)
        assert result == []

    def test_multiple_markets_mixed(self):
        """Only expired markets appear in the result list."""
        ctx_expired = make_tick_context(expiry_time=100.0)
        ctx_active = make_tick_context(expiry_time=500.0)
        ctx_disabled = make_tick_context(expiry_time=0.0)
        tracked = {
            "expired": ctx_expired,
            "active": ctx_active,
            "disabled": ctx_disabled,
        }
        result = evaluate_expiry(tracked, now=200.0)
        assert result == ["expired"]

    def test_empty_tracked_markets(self):
        """An empty dict returns an empty list."""
        assert evaluate_expiry({}, now=1000.0) == []


# ═══════════════════════════════════════════════════════════════════════════
# evaluate_exit
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluateExit:
    """Tests for evaluate_exit()."""

    def test_returns_none_for_idle_state(self):
        """IDLE markets are not eligible for exit evaluation."""
        ctx = make_tick_context(state=MarketState.IDLE)
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_for_entering_state(self):
        """ENTERING markets are not eligible for exit evaluation."""
        ctx = make_tick_context(state=MarketState.ENTERING)
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_for_exiting_state(self):
        """EXITING markets are not eligible for exit evaluation."""
        ctx = make_tick_context(state=MarketState.EXITING)
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_for_cooldown_state(self):
        """COOLDOWN markets are not eligible for exit evaluation."""
        ctx = make_tick_context(state=MarketState.COOLDOWN)
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_when_entry_price_is_none(self):
        """Missing entry_price should cause early return of None."""
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=None,
            entry_time=1_700_000_000.0,
            entry_side="YES",
        )
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_when_entry_time_is_none(self):
        """Missing entry_time should cause early return of None."""
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=None,
            entry_side="YES",
        )
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_returns_none_when_both_entry_fields_missing(self):
        """Both entry_price and entry_time None should return None."""
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=None,
            entry_time=None,
            entry_side="YES",
        )
        assert evaluate_exit(ctx, position_price=0.55, now=1_700_000_100.0) is None

    def test_delegates_to_check_exit_stop_loss(self):
        """When ENTERED with valid entry fields, a stop-loss should trigger."""
        cfg = make_strategy_config(stop_loss_delta=0.04, stop_loss_enabled=True)
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_000.0,
            entry_side="YES",
            strategy_config=cfg,
            yes_price=0.45,
        )
        # Price dropped from 0.50 to 0.45 -- delta of 0.05 exceeds 0.04 stop.
        result = evaluate_exit(ctx, position_price=0.45, now=1_700_000_050.0)
        assert result is not None
        assert isinstance(result, ExitSignal)
        assert result.reason == "stop_loss"

    def test_delegates_to_check_exit_take_profit(self):
        """When price has risen enough, take-profit should trigger."""
        cfg = make_strategy_config(
            take_profit_delta=0.06,
            take_profit_enabled=True,
            stop_loss_enabled=False,
        )
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_000.0,
            entry_side="YES",
            strategy_config=cfg,
        )
        # Price rose from 0.50 to 0.57 -- delta of 0.07 exceeds 0.06 target.
        result = evaluate_exit(ctx, position_price=0.57, now=1_700_000_050.0)
        assert result is not None
        assert result.reason == "take_profit"

    def test_delegates_to_check_exit_time_exit(self):
        """When max hold time exceeded, time_exit should trigger."""
        cfg = make_strategy_config(
            max_hold_seconds=240.0,
            time_exit_enabled=True,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_000.0,
            entry_side="YES",
            strategy_config=cfg,
        )
        # 300 seconds elapsed exceeds 240 second max hold.
        result = evaluate_exit(ctx, position_price=0.50, now=1_700_000_300.0)
        assert result is not None
        assert result.reason == "time_exit"

    def test_returns_none_when_no_exit_triggers(self):
        """When no exit rule fires, should return None."""
        cfg = make_strategy_config(
            stop_loss_delta=0.04,
            take_profit_delta=0.06,
            max_hold_seconds=240.0,
        )
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_000.0,
            entry_side="YES",
            strategy_config=cfg,
        )
        # Price unchanged, only 10 seconds in -- nothing triggers.
        result = evaluate_exit(ctx, position_price=0.50, now=1_700_000_010.0)
        assert result is None

    def test_entry_side_defaults_to_yes_when_none(self):
        """When entry_side is None, it should default to YES."""
        cfg = make_strategy_config(
            stop_loss_delta=0.04,
            stop_loss_enabled=True,
        )
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_000.0,
            entry_side=None,  # None should default to "YES"
            strategy_config=cfg,
        )
        # Price dropped enough to trigger stop-loss from YES perspective.
        result = evaluate_exit(ctx, position_price=0.45, now=1_700_000_050.0)
        assert result is not None
        assert result.reason == "stop_loss"


# ═══════════════════════════════════════════════════════════════════════════
# evaluate_entry
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluateEntry:
    """Tests for evaluate_entry()."""

    def test_returns_none_for_entered_state(self):
        """ENTERED markets are not eligible for new entries."""
        ctx = make_tick_context(state=MarketState.ENTERED)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_none_for_entering_state(self):
        """ENTERING markets are not eligible for new entries."""
        ctx = make_tick_context(state=MarketState.ENTERING)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_none_for_exiting_state(self):
        """EXITING markets are not eligible for new entries."""
        ctx = make_tick_context(state=MarketState.EXITING)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_none_for_cooldown_state(self):
        """COOLDOWN markets are not eligible for new entries."""
        ctx = make_tick_context(state=MarketState.COOLDOWN)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_none_when_btc_open_is_zero(self):
        """btc_open <= 0 should return None immediately."""
        ctx = make_tick_context(state=MarketState.IDLE)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=0.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_none_when_btc_open_is_negative(self):
        """btc_open < 0 should return None immediately."""
        ctx = make_tick_context(state=MarketState.IDLE)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=-1.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_returns_trade_intent_on_yes_signal(self):
        """A strong upward BTC move should produce a YES TradeIntent."""
        # BTC went up significantly: 67000 -> 67500 (~0.75%)
        # With default SCALE_FACTOR=0.01, implied_prob = 0.5 + 0.00746/0.01 = ~1.246 -> clamped to 0.99
        # YES edge = 0.99 - 0.50 = 0.49 >> threshold
        ctx = make_tick_context(
            state=MarketState.IDLE,
            yes_price=0.50,
            no_price=0.50,
            market_id="0xtest_market_id",
            token_id="token_abc123",
            tick_timestamp_ms=1_700_000_000_000,
        )
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is not None
        assert isinstance(result, TradeIntent)
        assert result.market_id == "0xtest_market_id"
        assert result.token_id == "token_abc123"
        assert result.side == "buy"
        assert result.outcome == "yes"
        assert result.size_usdc == 5.0
        assert result.signal.should_trade is True
        assert result.signal.direction == "YES"
        assert result.signal_price == ctx.yes_price
        assert result.tick_timestamp_ms == ctx.tick_timestamp_ms

    def test_returns_trade_intent_on_no_signal(self):
        """A strong downward BTC move should produce a NO TradeIntent."""
        # BTC went down: 67000 -> 66500 (~-0.75%)
        ctx = make_tick_context(
            state=MarketState.IDLE,
            yes_price=0.50,
            no_price=0.50,
        )
        result = evaluate_entry(
            ctx,
            btc_current=66_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is not None
        assert isinstance(result, TradeIntent)
        assert result.outcome == "no"
        assert result.signal.direction == "NO"
        # For NO direction, signal_price should be the no_price
        assert result.signal_price == ctx.no_price

    def test_returns_none_when_signal_below_threshold(self):
        """When edge is below threshold, should return None."""
        # Tiny BTC move: 67000 -> 67001 (~0.0015%)
        # This is below MIN_BTC_PCT_CHANGE, so should_trade=False
        ctx = make_tick_context(
            state=MarketState.IDLE,
            yes_price=0.50,
            no_price=0.50,
        )
        result = evaluate_entry(
            ctx,
            btc_current=67_001.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_handles_value_error_from_compute_signal(self, monkeypatch):
        """If compute_signal raises ValueError, evaluate_entry returns None."""
        # Monkeypatch compute_signal to raise ValueError
        def _raise(*args, **kwargs):
            raise ValueError("bad input")

        monkeypatch.setattr("updown.decisions.compute_signal", _raise)

        ctx = make_tick_context(state=MarketState.IDLE)
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is None

    def test_trade_intent_market_snapshot_fields(self):
        """The MarketSnapshot embedded in TradeIntent should match the TickContext."""
        ctx = make_tick_context(
            state=MarketState.IDLE,
            yes_price=0.50,
            no_price=0.50,
            market_id="0xsnap_test",
            question="Test question?",
            token_id="tok_snap",
            tick_timestamp_ms=1_700_000_000_000,
        )
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is not None
        snap = result.market
        assert snap.market_id == "0xsnap_test"
        assert snap.question == "Test question?"
        assert snap.token_id == "tok_snap"
        assert snap.yes_price == 0.50
        assert snap.no_price == 0.50
        assert snap.spread == abs(0.50 - 0.50)
        assert snap.timestamp_ms == 1_700_000_000_000

    def test_trade_intent_reason_format(self):
        """The reason string should contain BTC, direction, edge, implied, and market."""
        ctx = make_tick_context(
            state=MarketState.IDLE,
            yes_price=0.50,
            no_price=0.50,
        )
        result = evaluate_entry(
            ctx,
            btc_current=67_500.0,
            btc_open=67_000.0,
            threshold=0.05,
            trade_amount_usdc=5.0,
            now=1_700_000_100.0,
        )
        assert result is not None
        assert "BTC" in result.reason
        assert "momentum" in result.reason
        assert "edge=" in result.reason
        assert "implied=" in result.reason
        assert "market=" in result.reason
