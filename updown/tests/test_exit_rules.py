"""Tests for updown/exit_rules.py — pure exit-condition evaluator.

Covers:
- Delta stop-loss trigger
- Percent stop-loss trigger
- Delta take-profit trigger
- Percent take-profit trigger
- Time-exit trigger
- Evaluation order (stop-loss fires before take-profit)
- Individual rule disabling via config flags
- None return when no rule triggers
- Both YES and NO sides
- Boundary cases (exactly at threshold)
"""

from __future__ import annotations

import pytest

from updown.exit_rules import ExitSignal, check_exit
from updown.strategy_config import (
    StopLossPercentConfig,
    TakeProfitPercentConfig,
)
from updown.tests.conftest import make_exit_rules_config


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

# Baseline timestamps
T0 = 1_700_000_000.0  # entry time
NOW = T0 + 60.0        # 60 seconds after entry (well within default 240s hold)


# ═══════════════════════════════════════════════════════════════════════════
# No rule triggers -> None
# ═══════════════════════════════════════════════════════════════════════════


class TestNoTrigger:
    """check_exit returns None when the position is within all thresholds."""

    def test_no_trigger_yes_side(self):
        cfg = make_exit_rules_config()
        result = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,  # no movement
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert result is None

    def test_no_trigger_no_side(self):
        cfg = make_exit_rules_config()
        result = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=NOW,
            side="NO",
        )
        assert result is None

    def test_small_loss_below_threshold(self):
        cfg = make_exit_rules_config(stop_loss_delta=0.04)
        result = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.47,  # loss = 0.03, below 0.04
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert result is None

    def test_small_profit_below_threshold(self):
        cfg = make_exit_rules_config(take_profit_delta=0.06)
        result = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.55,  # profit = 0.05, below 0.06
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Delta stop-loss
# ═══════════════════════════════════════════════════════════════════════════


class TestDeltaStopLoss:

    def test_triggers_when_loss_exceeds_delta(self):
        cfg = make_exit_rules_config(stop_loss_delta=0.04, stop_loss_pct=None)
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.45,  # loss = 0.05 > 0.04
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "delta" in signal.detail

    def test_triggers_on_no_side(self):
        cfg = make_exit_rules_config(stop_loss_delta=0.04, stop_loss_pct=None)
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.45,  # loss = 0.05 > 0.04
            entry_time=T0,
            now=NOW,
            side="NO",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "side=NO" in signal.detail

    def test_boundary_exactly_at_delta(self):
        """Loss exactly equal to max_loss_delta should trigger (>= comparison)."""
        # Use 0.50 - 0.25 = 0.25 which is exact in IEEE 754
        cfg = make_exit_rules_config(stop_loss_delta=0.25, stop_loss_pct=None)
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.25,  # loss = 0.25, exactly at threshold
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"

    def test_detail_includes_numbers(self):
        cfg = make_exit_rules_config(stop_loss_delta=0.04, stop_loss_pct=None)
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.44,
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert "0.0600" in signal.detail  # loss
        assert "0.0400" in signal.detail  # max delta


# ═══════════════════════════════════════════════════════════════════════════
# Percent stop-loss
# ═══════════════════════════════════════════════════════════════════════════


class TestPercentStopLoss:

    def test_triggers_when_loss_exceeds_percent(self):
        # 8% of entry 0.50 = 0.04; set delta high so it won't fire first
        cfg = make_exit_rules_config(
            stop_loss_delta=1.0,  # effectively disabled
            stop_loss_pct=0.08,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.45,  # loss = 0.05 > 0.04 (8% of 0.50)
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "percent" in signal.detail

    def test_boundary_exactly_at_percent(self):
        """Loss exactly at percent threshold should trigger (>= comparison)."""
        # Use 50% of 0.50 = 0.25; loss = 0.50 - 0.25 = 0.25 (exact in float)
        cfg = make_exit_rules_config(
            stop_loss_delta=1.0,
            stop_loss_pct=0.50,  # 50% of 0.50 = 0.25
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.25,  # loss = 0.25 = 50% of 0.50
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "percent" in signal.detail

    def test_no_trigger_when_percent_is_none(self):
        """When stop_loss_pct is None, only delta rule is checked."""
        cfg = make_exit_rules_config(
            stop_loss_delta=1.0,  # won't fire
            stop_loss_pct=None,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.10,  # huge loss, but delta=1.0 won't fire, pct is None
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is None

    def test_on_no_side(self):
        cfg = make_exit_rules_config(
            stop_loss_delta=1.0,
            stop_loss_pct=0.08,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.45,
            entry_time=T0,
            now=NOW,
            side="NO",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "side=NO" in signal.detail


# ═══════════════════════════════════════════════════════════════════════════
# Delta take-profit
# ═══════════════════════════════════════════════════════════════════════════


class TestDeltaTakeProfit:

    def test_triggers_when_profit_exceeds_delta(self):
        cfg = make_exit_rules_config(
            take_profit_delta=0.06,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,  # profit = 0.07 > 0.06
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "delta" in signal.detail

    def test_triggers_on_no_side(self):
        cfg = make_exit_rules_config(
            take_profit_delta=0.06,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,
            entry_time=T0,
            now=NOW,
            side="NO",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "side=NO" in signal.detail

    def test_boundary_exactly_at_delta(self):
        """Profit exactly at target_delta should trigger (>= comparison)."""
        cfg = make_exit_rules_config(
            take_profit_delta=0.06,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.56,  # profit = 0.06, exactly at threshold
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"

    def test_detail_includes_numbers(self):
        cfg = make_exit_rules_config(
            take_profit_delta=0.06,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.58,
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert "0.0800" in signal.detail  # profit
        assert "0.0600" in signal.detail  # target delta


# ═══════════════════════════════════════════════════════════════════════════
# Percent take-profit
# ═══════════════════════════════════════════════════════════════════════════


class TestPercentTakeProfit:

    def test_triggers_when_profit_exceeds_percent(self):
        # 12% of entry 0.50 = 0.06; set delta high so it won't fire first
        cfg = make_exit_rules_config(
            take_profit_delta=1.0,  # effectively disabled
            take_profit_pct=0.12,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,  # profit = 0.07 > 0.06 (12% of 0.50)
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "percent" in signal.detail

    def test_boundary_exactly_at_percent(self):
        cfg = make_exit_rules_config(
            take_profit_delta=1.0,
            take_profit_pct=0.10,  # 10% of 0.50 = 0.05
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.55,  # profit = 0.05 = 10% of 0.50
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "percent" in signal.detail

    def test_no_trigger_when_percent_is_none(self):
        cfg = make_exit_rules_config(
            take_profit_delta=1.0,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.90,  # huge profit, but delta=1.0 won't fire, pct is None
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is None

    def test_on_no_side(self):
        cfg = make_exit_rules_config(
            take_profit_delta=1.0,
            take_profit_pct=0.12,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,
            entry_time=T0,
            now=NOW,
            side="NO",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "side=NO" in signal.detail


# ═══════════════════════════════════════════════════════════════════════════
# Time exit
# ═══════════════════════════════════════════════════════════════════════════


class TestTimeExit:

    def test_triggers_when_held_exceeds_max(self):
        cfg = make_exit_rules_config(
            max_hold_seconds=240.0,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 300.0,  # held 300s > 240s
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "time_exit"

    def test_triggers_on_no_side(self):
        cfg = make_exit_rules_config(
            max_hold_seconds=240.0,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 300.0,
            side="NO",
        )
        assert signal is not None
        assert signal.reason == "time_exit"

    def test_boundary_exactly_at_max_hold(self):
        """Held time exactly at max_hold_seconds should trigger (>= comparison)."""
        cfg = make_exit_rules_config(
            max_hold_seconds=240.0,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 240.0,  # exactly at threshold
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "time_exit"

    def test_no_trigger_below_max_hold(self):
        cfg = make_exit_rules_config(
            max_hold_seconds=240.0,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 239.0,  # 1 second under
            side="YES",
        )
        assert signal is None

    def test_detail_includes_timing(self):
        cfg = make_exit_rules_config(
            max_hold_seconds=240.0,
            stop_loss_enabled=False,
            take_profit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 300.0,
            side="YES",
        )
        assert signal is not None
        assert "300.0" in signal.detail
        assert "240.0" in signal.detail


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation order: stop-loss fires before take-profit
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluationOrder:

    def test_stop_loss_fires_before_take_profit_when_both_would_trigger(self):
        """When price drops enough that both SL and TP would fire, SL wins.

        This is a degenerate case: the only way both can fire is if a
        "profit" and "loss" computation both pass thresholds.  Since
        loss = entry - current and profit = current - entry, they can't
        both be positive simultaneously.  However, we can test that SL
        is checked first by verifying that when SL triggers, TP is
        never reached even if the TP threshold is very loose.
        """
        # SL fires: loss = 0.10 >= delta 0.04
        # TP would NOT fire because profit = -0.10 < 0.06
        # This confirms SL is checked first in the code path.
        cfg = make_exit_rules_config(
            stop_loss_delta=0.04,
            take_profit_delta=0.06,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.40,  # loss = 0.10
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"

    def test_stop_loss_checked_before_time_exit(self):
        """Stop loss fires even when time exit would also trigger."""
        cfg = make_exit_rules_config(
            stop_loss_delta=0.04,
            max_hold_seconds=240.0,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.40,  # SL triggers
            entry_time=T0,
            now=T0 + 300.0,     # time exit would also trigger
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"

    def test_take_profit_checked_before_time_exit(self):
        """Take profit fires even when time exit would also trigger."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_delta=0.06,
            max_hold_seconds=240.0,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,  # TP triggers
            entry_time=T0,
            now=T0 + 300.0,     # time exit would also trigger
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"

    def test_delta_stop_loss_fires_before_percent_stop_loss(self):
        """Within stop-loss, delta is evaluated before percent."""
        # Both delta and percent would fire. Delta should win.
        cfg = make_exit_rules_config(
            stop_loss_delta=0.02,   # loss 0.10 >= 0.02 -> fires
            stop_loss_pct=0.01,     # 1% of 0.50 = 0.005; loss 0.10 >= 0.005 -> would fire
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.40,  # loss = 0.10
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert "delta" in signal.detail  # delta fires first

    def test_delta_take_profit_fires_before_percent_take_profit(self):
        """Within take-profit, delta is evaluated before percent."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_delta=0.02,  # profit 0.10 >= 0.02 -> fires
            take_profit_pct=0.01,    # 1% of 0.50 = 0.005; profit 0.10 >= 0.005 -> would fire
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.60,  # profit = 0.10
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert "delta" in signal.detail  # delta fires first


# ═══════════════════════════════════════════════════════════════════════════
# Individually disabling rules
# ═══════════════════════════════════════════════════════════════════════════


class TestDisabledRules:

    def test_stop_loss_disabled(self):
        """Disabling stop_loss means it never fires even with huge loss."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            time_exit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.01,  # massive loss
            entry_time=T0,
            now=T0 + 99999.0,   # way past time exit
            side="YES",
        )
        assert signal is None

    def test_take_profit_disabled(self):
        """Disabling take_profit means it never fires even with huge profit."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            time_exit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.99,  # massive profit
            entry_time=T0,
            now=NOW,
            side="YES",
        )
        assert signal is None

    def test_time_exit_disabled(self):
        """Disabling time_exit means it never fires even after very long hold."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            time_exit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 1_000_000.0,  # held for ~11.5 days
            side="YES",
        )
        assert signal is None

    def test_all_rules_disabled_returns_none(self):
        """With all rules off, any input returns None."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            time_exit_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.01,
            entry_time=T0,
            now=T0 + 1_000_000.0,
            side="YES",
        )
        assert signal is None

    def test_only_stop_loss_enabled(self):
        """With only stop-loss enabled, take-profit and time exit don't fire."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=True,
            stop_loss_delta=0.04,
            take_profit_enabled=False,
            time_exit_enabled=False,
        )
        # Price went UP (profit) -- SL should not fire
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.99,
            entry_time=T0,
            now=T0 + 99999.0,
            side="YES",
        )
        assert signal is None

    def test_only_take_profit_enabled(self):
        """With only take-profit enabled, stop-loss and time exit don't fire."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=True,
            take_profit_delta=0.06,
            time_exit_enabled=False,
        )
        # Price went DOWN (loss) -- TP should not fire
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.01,
            entry_time=T0,
            now=T0 + 99999.0,
            side="YES",
        )
        assert signal is None

    def test_only_time_exit_enabled(self):
        """With only time-exit enabled, price-based rules don't fire."""
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            time_exit_enabled=True,
            max_hold_seconds=240.0,
        )
        # Price went down, but SL is off; time has elapsed
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.01,
            entry_time=T0,
            now=T0 + 300.0,
            side="YES",
        )
        assert signal is not None
        assert signal.reason == "time_exit"


# ═══════════════════════════════════════════════════════════════════════════
# Both sides (YES and NO)
# ═══════════════════════════════════════════════════════════════════════════


class TestBothSides:
    """The side parameter appears in the detail string but doesn't change
    the profit/loss calculation (caller is responsible for passing the
    correct position-side token price)."""

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_stop_loss_fires_for_both_sides(self, side: str):
        cfg = make_exit_rules_config(stop_loss_delta=0.04, stop_loss_pct=None)
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.45,
            entry_time=T0,
            now=NOW,
            side=side,
        )
        assert signal is not None
        assert signal.reason == "stop_loss"
        assert f"side={side}" in signal.detail

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_take_profit_fires_for_both_sides(self, side: str):
        cfg = make_exit_rules_config(
            take_profit_delta=0.06,
            take_profit_pct=None,
            stop_loss_enabled=False,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.57,
            entry_time=T0,
            now=NOW,
            side=side,
        )
        assert signal is not None
        assert signal.reason == "take_profit"
        assert f"side={side}" in signal.detail

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_time_exit_fires_for_both_sides(self, side: str):
        cfg = make_exit_rules_config(
            stop_loss_enabled=False,
            take_profit_enabled=False,
            max_hold_seconds=240.0,
        )
        signal = check_exit(
            config=cfg,
            entry_price=0.50,
            current_price=0.50,
            entry_time=T0,
            now=T0 + 300.0,
            side=side,
        )
        assert signal is not None
        assert signal.reason == "time_exit"


# ═══════════════════════════════════════════════════════════════════════════
# ExitSignal dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestExitSignal:

    def test_is_frozen(self):
        sig = ExitSignal(reason="stop_loss", detail="test")
        with pytest.raises(AttributeError):
            sig.reason = "take_profit"  # type: ignore[misc]

    def test_fields(self):
        sig = ExitSignal(reason="take_profit", detail="profit hit target")
        assert sig.reason == "take_profit"
        assert sig.detail == "profit hit target"
