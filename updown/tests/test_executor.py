"""Tests for updown/executor.py.

Covers:
- check_slippage(): within tolerance, exceeding tolerance, exact boundary
- build_exit_intent(): correct TradeIntent fields for YES/NO, ValueError on no position
- _compute_realized_delta(): YES side, NO side, None entry
- place_order() dry mode: synthetic OrderResult, _persist_trade called
- place_order() slippage rejection path
- drain_latency_stats() / record_latency_sample(): avg/max/count, empty, drain clears
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from common import config
from updown import executor
from updown.executor import (
    _compute_realized_delta,
    build_exit_intent,
    check_slippage,
    drain_latency_stats,
    place_order,
    record_latency_sample,
)
from updown.exit_rules import ExitSignal
from updown.types import MarketState

# Import conftest factories directly so they are available without fixture injection.
from updown.tests.conftest import (
    make_tracked_market,
    make_trade_intent,
)


# ═══════════════════════════════════════════════════════════════════════════
# check_slippage
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckSlippage:
    """Pure function: abs(signal - execution) > tolerance."""

    def test_within_tolerance_returns_false(self):
        assert check_slippage(0.50, 0.51, 0.02) is False

    def test_exceeding_tolerance_returns_true(self):
        assert check_slippage(0.50, 0.55, 0.02) is True

    def test_exact_boundary_returns_false(self):
        """Strict > means exactly at the boundary should NOT reject."""
        # Use 0.50 and 0.75 with tolerance 0.25 to avoid float precision issues.
        assert check_slippage(0.50, 0.75, 0.25) is False

    def test_negative_movement(self):
        """Price moved down -- abs delta still compared."""
        assert check_slippage(0.60, 0.55, 0.02) is True

    def test_zero_tolerance(self):
        """Zero tolerance rejects any price movement."""
        assert check_slippage(0.50, 0.50, 0.0) is False
        assert check_slippage(0.50, 0.5001, 0.0) is True


# ═══════════════════════════════════════════════════════════════════════════
# build_exit_intent
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildExitIntent:
    """Construct sell-side TradeIntent from a tracked position."""

    def test_yes_position_exit(self):
        tracked = make_tracked_market(
            state=MarketState.ENTERED,
            entry_price=0.45,
            entry_time=1_700_000_100.0,
            entry_side="yes",
            entry_size_usdc=5.0,
        )
        exit_signal = ExitSignal(reason="take_profit", detail="target delta reached")

        intent = build_exit_intent(tracked, exit_signal, current_price=0.55)

        assert intent.side == "sell"
        assert intent.outcome == "yes"
        assert intent.size_usdc == 5.0
        assert intent.market_id == tracked.condition_id
        assert intent.token_id == tracked.asset_ids[0]
        assert "EXIT" in intent.reason
        assert "take_profit" in intent.reason
        # For YES position, signal_price should be current_price
        assert intent.signal_price == 0.55

    def test_no_position_exit(self):
        tracked = make_tracked_market(
            state=MarketState.ENTERED,
            entry_price=0.55,
            entry_time=1_700_000_100.0,
            entry_side="no",
            entry_size_usdc=5.0,
        )
        exit_signal = ExitSignal(reason="stop_loss", detail="max loss delta breached")

        intent = build_exit_intent(tracked, exit_signal, current_price=0.60)

        assert intent.side == "sell"
        assert intent.outcome == "no"
        assert intent.size_usdc == 5.0
        # For NO position, signal_price should be the resolved NO price
        assert intent.signal_price == 0.40  # 1.0 - 0.60

    def test_no_position_exit_with_explicit_no_price(self):
        tracked = make_tracked_market(
            state=MarketState.ENTERED,
            entry_price=0.55,
            entry_time=1_700_000_100.0,
            entry_side="no",
            entry_size_usdc=5.0,
        )
        exit_signal = ExitSignal(reason="time_exit", detail="max hold exceeded")

        intent = build_exit_intent(
            tracked, exit_signal, current_price=0.60, no_price=0.38
        )

        # Explicit no_price used instead of 1.0 - current_price
        assert intent.signal_price == 0.38

    def test_raises_when_no_open_position(self):
        tracked = make_tracked_market(state=MarketState.IDLE)
        exit_signal = ExitSignal(reason="stop_loss", detail="n/a")

        with pytest.raises(ValueError, match="no open position"):
            build_exit_intent(tracked, exit_signal, current_price=0.50)

    def test_tick_timestamp_forwarded(self):
        tracked = make_tracked_market(
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_time=1_700_000_100.0,
            entry_side="yes",
            entry_size_usdc=5.0,
        )
        exit_signal = ExitSignal(reason="time_exit", detail="expired")

        intent = build_exit_intent(
            tracked, exit_signal, current_price=0.55, tick_timestamp_ms=999
        )

        assert intent.tick_timestamp_ms == 999


# ═══════════════════════════════════════════════════════════════════════════
# _compute_realized_delta
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeRealizedDelta:
    """P&L delta: YES = exit - entry, NO = entry - exit."""

    def test_yes_side_profit(self):
        assert _compute_realized_delta("yes", 0.40, 0.55) == pytest.approx(0.15)

    def test_yes_side_loss(self):
        assert _compute_realized_delta("yes", 0.55, 0.40) == pytest.approx(-0.15)

    def test_no_side_profit(self):
        # NO profit when price drops: entry(high) - exit(low)
        assert _compute_realized_delta("no", 0.60, 0.45) == pytest.approx(0.15)

    def test_no_side_loss(self):
        assert _compute_realized_delta("no", 0.40, 0.55) == pytest.approx(-0.15)

    def test_none_entry_returns_none(self):
        assert _compute_realized_delta("yes", None, 0.55) is None

    def test_case_insensitive_yes(self):
        assert _compute_realized_delta("YES", 0.40, 0.55) == pytest.approx(0.15)


# ═══════════════════════════════════════════════════════════════════════════
# Latency stats
# ═══════════════════════════════════════════════════════════════════════════


class TestLatencyStats:
    """record_latency_sample / drain_latency_stats interaction."""

    def test_empty_returns_zeros(self):
        avg, mx, count = drain_latency_stats()
        assert (avg, mx, count) == (0, 0, 0)

    def test_single_sample(self):
        record_latency_sample(42)
        avg, mx, count = drain_latency_stats()
        assert avg == 42
        assert mx == 42
        assert count == 1

    def test_multiple_samples(self):
        record_latency_sample(10)
        record_latency_sample(20)
        record_latency_sample(30)
        avg, mx, count = drain_latency_stats()
        assert avg == 20
        assert mx == 30
        assert count == 3

    def test_drain_clears_buffer(self):
        record_latency_sample(100)
        drain_latency_stats()
        # Second drain should be empty
        avg, mx, count = drain_latency_stats()
        assert (avg, mx, count) == (0, 0, 0)

    def test_integer_division(self):
        """avg uses integer division."""
        record_latency_sample(10)
        record_latency_sample(11)
        avg, mx, count = drain_latency_stats()
        assert avg == 10  # (10+11)//2 = 10


# ═══════════════════════════════════════════════════════════════════════════
# place_order — dry mode
# ═══════════════════════════════════════════════════════════════════════════


class TestPlaceOrderDryMode:
    """Dry mode: no real API calls, synthetic OrderResult."""

    @pytest.mark.asyncio
    async def test_dry_mode_returns_synthetic_result(self, monkeypatch, tmp_data_dir):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.05)

        intent = make_trade_intent(side="buy", outcome="yes", size_usdc=5.0)

        result = await place_order(
            intent, edge=0.05, implied_prob=0.55, market_price=0.50
        )

        assert result.success is True
        assert result.order_id.startswith("dry-")
        assert result.filled_price == 0.50
        assert result.filled_size == 5.0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_dry_mode_calls_persist_trade(self, monkeypatch, tmp_data_dir):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.05)

        intent = make_trade_intent(side="buy", outcome="yes", size_usdc=5.0)

        with patch(
            "updown.executor.atomic_append_to_json_list"
        ) as mock_append:
            result = await place_order(
                intent, edge=0.05, implied_prob=0.55, market_price=0.50
            )

            # _persist_trade calls atomic_append_to_json_list
            assert mock_append.called
            call_args = mock_append.call_args
            record = call_args[0][1]
            assert record["dry_mode"] is True
            assert record["status"] == "dry"
            assert record["direction"] == "buy"

    @pytest.mark.asyncio
    async def test_dry_mode_sell_includes_exit_fields(self, monkeypatch, tmp_data_dir):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.10)

        intent = make_trade_intent(side="sell", outcome="yes", size_usdc=5.0)

        with patch(
            "updown.executor.atomic_append_to_json_list"
        ) as mock_append:
            result = await place_order(
                intent,
                edge=0.0,
                implied_prob=0.55,
                market_price=0.55,
                exit_reason="take_profit",
                entry_price=0.45,
                hold_duration_s=120.5,
            )

            assert result.success is True
            # First call is the trade record, second is PnL
            trade_record = mock_append.call_args_list[0][0][1]
            assert trade_record["exit_reason"] == "take_profit"
            assert trade_record["entry_price"] == 0.45
            assert trade_record["hold_duration_s"] == 120.5
            assert trade_record["realized_delta"] is not None

    @pytest.mark.asyncio
    async def test_dry_mode_records_latency(self, monkeypatch, tmp_data_dir):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.05)

        intent = make_trade_intent(
            side="buy", outcome="yes", tick_timestamp_ms=1000
        )

        with patch("updown.executor.time") as mock_time:
            # Simulate current time as 1500ms (epoch)
            mock_time.time.return_value = 1.5
            result = await place_order(
                intent, edge=0.05, implied_prob=0.55, market_price=0.50
            )

        # Latency = 1500 - 1000 = 500ms should have been recorded
        avg, mx, count = drain_latency_stats()
        assert count == 1
        assert avg == 500


# ═══════════════════════════════════════════════════════════════════════════
# place_order — slippage rejection
# ═══════════════════════════════════════════════════════════════════════════


class TestPlaceOrderSlippageRejection:
    """Slippage guard rejects orders when price moved too far."""

    @pytest.mark.asyncio
    async def test_buy_slippage_rejection(self, monkeypatch, tmp_data_dir):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.01)

        intent = make_trade_intent(
            side="buy", outcome="yes", signal_price=0.50
        )

        result = await place_order(
            intent, edge=0.05, implied_prob=0.55, market_price=0.55
        )

        assert result.success is False
        assert "slippage exceeded" in result.error
        assert executor.slippage_rejections == 1

    @pytest.mark.asyncio
    async def test_sell_uses_wider_tolerance(self, monkeypatch, tmp_data_dir):
        """Sell-side tolerance is multiplied by _EXIT_SLIPPAGE_MULTIPLIER (2x)."""
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.02)

        # Delta = 0.03, base tolerance = 0.02, exit tolerance = 0.04
        intent = make_trade_intent(
            side="sell", outcome="yes", signal_price=0.50
        )

        result = await place_order(
            intent, edge=0.0, implied_prob=0.55, market_price=0.53
        )

        # 0.03 <= 0.04 so should pass (not rejected)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_sell_slippage_rejection_when_exceeds_doubled(
        self, monkeypatch, tmp_data_dir
    ):
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.02)

        # Delta = 0.05, exit tolerance = 0.04
        intent = make_trade_intent(
            side="sell", outcome="yes", signal_price=0.50
        )

        result = await place_order(
            intent, edge=0.0, implied_prob=0.55, market_price=0.55
        )

        assert result.success is False
        assert "slippage exceeded" in result.error

    @pytest.mark.asyncio
    async def test_no_slippage_check_when_signal_price_none(
        self, monkeypatch, tmp_data_dir
    ):
        """When signal_price is None, slippage guard is skipped."""
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.001)

        intent = make_trade_intent(
            side="buy", outcome="yes", signal_price=None
        )

        result = await place_order(
            intent, edge=0.05, implied_prob=0.55, market_price=0.99
        )

        assert result.success is True
        assert executor.slippage_rejections == 0

    @pytest.mark.asyncio
    async def test_no_slippage_check_when_market_price_zero(
        self, monkeypatch, tmp_data_dir
    ):
        """When market_price=0, slippage guard is skipped."""
        monkeypatch.setattr(config, "UPDOWN_DRY_MODE", True)
        monkeypatch.setattr(config, "UPDOWN_SLIPPAGE_TOLERANCE", 0.001)

        intent = make_trade_intent(
            side="buy", outcome="yes", signal_price=0.50
        )

        result = await place_order(
            intent, edge=0.05, implied_prob=0.55, market_price=0.0
        )

        assert result.success is True
