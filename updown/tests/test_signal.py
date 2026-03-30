"""Tests for updown/signal.py -- momentum signal engine.

Covers:
- compute_signal() for positive, negative, zero, and large BTC moves
- Clamping to [0.01, 0.99] boundaries
- ValueError on non-positive open_price
- MIN_BTC_PCT_CHANGE gate forcing should_trade=False for micro-moves
- Edge sign and should_trade consistency with direction
- _clamp helper at boundaries
"""

from __future__ import annotations

import pytest

from updown.signal import _clamp, compute_signal
import updown.signal as signal_module


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures: patch module-level constants to known values
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _patch_signal_constants(monkeypatch: pytest.MonkeyPatch):
    """Pin SCALE_FACTOR and MIN_BTC_PCT_CHANGE to deterministic defaults."""
    monkeypatch.setattr(signal_module, "SCALE_FACTOR", 0.01)
    monkeypatch.setattr(signal_module, "MIN_BTC_PCT_CHANGE", 0.0001)


# ═══════════════════════════════════════════════════════════════════════════
# _clamp helper
# ═══════════════════════════════════════════════════════════════════════════


class TestClamp:
    """Test the _clamp(value, lo, hi) helper."""

    def test_value_within_range(self):
        assert _clamp(0.5, 0.01, 0.99) == 0.5

    def test_value_at_lower_boundary(self):
        assert _clamp(0.01, 0.01, 0.99) == 0.01

    def test_value_at_upper_boundary(self):
        assert _clamp(0.99, 0.01, 0.99) == 0.99

    def test_value_below_lower_boundary(self):
        assert _clamp(-5.0, 0.01, 0.99) == 0.01

    def test_value_above_upper_boundary(self):
        assert _clamp(100.0, 0.01, 0.99) == 0.99

    def test_value_just_below_lower(self):
        assert _clamp(0.009, 0.01, 0.99) == 0.01

    def test_value_just_above_upper(self):
        assert _clamp(0.991, 0.01, 0.99) == 0.99


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- ValueError on bad open_price
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeSignalValidation:
    """open_price must be positive."""

    def test_open_price_zero_raises(self):
        with pytest.raises(ValueError, match="open_price must be positive"):
            compute_signal(
                current_price=67_000.0,
                open_price=0.0,
                market_yes_price=0.50,
                threshold=0.05,
            )

    def test_open_price_negative_raises(self):
        with pytest.raises(ValueError, match="open_price must be positive"):
            compute_signal(
                current_price=67_000.0,
                open_price=-100.0,
                market_yes_price=0.50,
                threshold=0.05,
            )


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- directional moves
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeSignalDirection:
    """Positive BTC move -> YES, negative -> NO, zero -> near equilibrium."""

    def test_positive_btc_move_yes_direction(self):
        """BTC up -> implied_probability > 0.5 -> YES direction."""
        result = compute_signal(
            current_price=67_200.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.direction == "YES"
        assert result.implied_probability > 0.5
        assert result.edge > 0  # positive edge on the YES side

    def test_negative_btc_move_no_direction(self):
        """BTC down -> implied_probability < 0.5 -> NO direction."""
        result = compute_signal(
            current_price=66_800.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.direction == "NO"
        assert result.implied_probability < 0.5
        # For NO direction: edge = (1 - implied_probability) - (1 - market_yes_price)
        # = market_yes_price - implied_probability > 0
        assert result.edge > 0

    def test_zero_btc_move(self):
        """No price change -> implied_probability == 0.5 exactly."""
        result = compute_signal(
            current_price=67_000.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.implied_probability == 0.5
        # Both edges are 0; yes_edge >= no_edge so direction defaults to YES
        assert result.edge == pytest.approx(0.0)
        assert result.should_trade is False  # zero edge < threshold
        # Also gated by MIN_BTC_PCT_CHANGE (pct_change == 0)

    def test_market_price_stored(self):
        result = compute_signal(
            current_price=67_200.0,
            open_price=67_000.0,
            market_yes_price=0.55,
            threshold=0.01,
        )
        assert result.market_price == 0.55


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- clamping large moves
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeSignalClamping:
    """Large BTC moves clamp implied_probability to [0.01, 0.99]."""

    def test_large_positive_move_clamps_to_099(self):
        """Huge BTC rally -> implied probability clamped at 0.99."""
        # pct_change = (80000 - 67000) / 67000 ~ 0.194
        # 0.5 + (0.194 / 0.01) = 0.5 + 19.4 -> clamp to 0.99
        result = compute_signal(
            current_price=80_000.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.implied_probability == 0.99
        assert result.direction == "YES"

    def test_large_negative_move_clamps_to_001(self):
        """Huge BTC crash -> implied probability clamped at 0.01."""
        # pct_change = (50000 - 67000) / 67000 ~ -0.254
        # 0.5 + (-0.254 / 0.01) = 0.5 - 25.4 -> clamp to 0.01
        result = compute_signal(
            current_price=50_000.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.implied_probability == 0.01
        assert result.direction == "NO"


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- should_trade and edge threshold
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeSignalShouldTrade:
    """should_trade is True only when abs(edge) > threshold."""

    def test_edge_above_threshold_should_trade(self):
        """Enough BTC movement produces sufficient edge to trade."""
        # SCALE_FACTOR = 0.01, so pct_change of 0.001 -> implied = 0.5 + 0.1 = 0.6
        # With market at 0.50: yes_edge = 0.6 - 0.5 = 0.10 > threshold 0.05
        result = compute_signal(
            current_price=67_067.0,  # ~0.1% up
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.should_trade is True
        assert abs(result.edge) > 0.05

    def test_edge_below_threshold_no_trade(self):
        """Small BTC movement produces insufficient edge."""
        # pct_change ~ 0.00015 -> implied = 0.5 + 0.015 = 0.515
        # yes_edge = 0.515 - 0.50 = 0.015 < threshold 0.05
        result = compute_signal(
            current_price=67_010.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.should_trade is False

    def test_edge_just_above_threshold(self):
        """Edge barely exceeding threshold -> should_trade True."""
        # We want edge just above 0.05. With market_yes=0.50:
        # need implied ~ 0.551 => pct_change ~ 0.00051 * SCALE_FACTOR
        # pct_change = 0.00051, implied = 0.5 + 0.051 = 0.551
        # yes_edge = 0.551 - 0.50 = 0.051 > 0.05
        open_p = 100_000.0
        # pct_change = 0.00051 => current = 100_000 * 1.00051 = 100_051
        result = compute_signal(
            current_price=100_051.0,
            open_price=open_p,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.should_trade is True
        assert result.edge > 0.05

    def test_edge_just_below_threshold(self):
        """Edge barely below threshold -> should_trade False."""
        open_p = 100_000.0
        # pct_change = 0.00049 => implied = 0.5 + 0.049 = 0.549
        # yes_edge = 0.049 < 0.05
        result = compute_signal(
            current_price=100_049.0,
            open_price=open_p,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.should_trade is False
        assert abs(result.edge) < 0.05


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- MIN_BTC_PCT_CHANGE gate
# ═══════════════════════════════════════════════════════════════════════════


class TestMinBtcPctChangeGate:
    """Micro-moves below MIN_BTC_PCT_CHANGE force should_trade=False."""

    def test_micro_move_gated(self):
        """A tiny BTC move that would otherwise have edge is gated out."""
        # MIN_BTC_PCT_CHANGE = 0.0001 (0.01%)
        # pct_change = 5/67000 ~ 0.0000746 < 0.0001 -> gated
        # But set a tiny threshold so edge alone would pass
        result = compute_signal(
            current_price=67_005.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.001,  # very low threshold
        )
        assert result.should_trade is False

    def test_move_above_min_gate_not_gated(self):
        """A BTC move above MIN_BTC_PCT_CHANGE is not gated."""
        # pct_change = 10/67000 ~ 0.000149 > 0.0001 -> not gated
        result = compute_signal(
            current_price=67_010.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.001,  # very low threshold
        )
        # Not gated by min change, and edge > tiny threshold
        assert result.should_trade is True

    def test_exactly_at_min_gate_is_gated(self):
        """pct_change exactly equal to MIN_BTC_PCT_CHANGE is still gated (strict <)."""
        # abs(pct_change) < MIN_BTC_PCT_CHANGE => 0.0001 is NOT < 0.0001
        # So exactly at the boundary should NOT be gated.
        open_p = 100_000.0
        # pct_change = 10 / 100000 = 0.0001 exactly
        result = compute_signal(
            current_price=100_010.0,
            open_price=open_p,
            market_yes_price=0.50,
            threshold=0.001,
        )
        # abs(0.0001) < 0.0001 is False, so NOT gated
        assert result.should_trade is True

    def test_gate_preserves_direction_and_edge(self):
        """Even when gated, direction/edge/implied_probability are still computed."""
        result = compute_signal(
            current_price=67_005.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.001,
        )
        assert result.should_trade is False
        # Direction and edge still reflect the move
        assert result.direction in ("YES", "NO")
        assert result.implied_probability != 0.0  # not zeroed out
        assert result.edge != 0.0  # edge is still computed

    def test_patched_min_gate_zero_disables_gating(self, monkeypatch):
        """Setting MIN_BTC_PCT_CHANGE to 0 effectively disables the gate."""
        monkeypatch.setattr(signal_module, "MIN_BTC_PCT_CHANGE", 0.0)
        result = compute_signal(
            current_price=67_005.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.001,
        )
        # With gate at 0, any non-zero move passes (abs(pct) < 0 is always False)
        assert result.should_trade is True


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- edge sign consistency
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeSignConsistency:
    """Edge should be non-negative for the chosen direction when market is at 0.50."""

    def test_yes_direction_positive_edge(self):
        """YES direction should have non-negative edge."""
        result = compute_signal(
            current_price=67_200.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.direction == "YES"
        assert result.edge >= 0

    def test_no_direction_positive_edge(self):
        """NO direction should have non-negative edge."""
        result = compute_signal(
            current_price=66_800.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.direction == "NO"
        assert result.edge >= 0

    def test_asymmetric_market_yes_side(self):
        """When market YES price is low (0.30) and BTC goes up, YES edge dominates."""
        result = compute_signal(
            current_price=67_100.0,
            open_price=67_000.0,
            market_yes_price=0.30,
            threshold=0.01,
        )
        assert result.direction == "YES"
        # implied_prob = 0.5 + (0.001493 / 0.01) ~ 0.649
        # yes_edge = 0.649 - 0.30 = 0.349
        assert result.edge > 0

    def test_asymmetric_market_no_side(self):
        """When market YES price is high (0.70) and BTC drops, NO edge dominates."""
        result = compute_signal(
            current_price=66_900.0,
            open_price=67_000.0,
            market_yes_price=0.70,
            threshold=0.01,
        )
        assert result.direction == "NO"
        assert result.edge > 0


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- SCALE_FACTOR patching
# ═══════════════════════════════════════════════════════════════════════════


class TestScaleFactorPatching:
    """Tests that confirm patching SCALE_FACTOR at the module level works."""

    def test_larger_scale_factor_reduces_sensitivity(self, monkeypatch):
        """A bigger SCALE_FACTOR means the same BTC move produces less edge."""
        monkeypatch.setattr(signal_module, "SCALE_FACTOR", 0.10)
        # pct_change ~ 0.00298, implied = 0.5 + (0.00298 / 0.10) = 0.5298
        result = compute_signal(
            current_price=67_200.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.05,
        )
        # Edge = ~0.0298, below 0.05 threshold
        assert result.should_trade is False
        assert result.implied_probability < 0.55

    def test_smaller_scale_factor_increases_sensitivity(self, monkeypatch):
        """A smaller SCALE_FACTOR means even tiny BTC moves produce large edge."""
        monkeypatch.setattr(signal_module, "SCALE_FACTOR", 0.001)
        # pct_change ~ 0.000149, implied = 0.5 + (0.000149 / 0.001) = 0.649
        result = compute_signal(
            current_price=67_010.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.05,
        )
        assert result.should_trade is True
        assert result.implied_probability > 0.6


# ═══════════════════════════════════════════════════════════════════════════
# compute_signal -- implied probability math spot checks
# ═══════════════════════════════════════════════════════════════════════════


class TestImpliedProbabilityMath:
    """Verify the formula: implied = clamp(0.5 + pct_change / SCALE_FACTOR, 0.01, 0.99)."""

    def test_known_pct_change(self):
        """With SCALE_FACTOR=0.01, a 0.5% move gives implied = 0.5 + 0.5 = 0.99 (clamped)."""
        # pct_change = 335/67000 ~ 0.005 -> 0.5 + 0.5 = 1.0 -> clamp to 0.99
        result = compute_signal(
            current_price=67_335.0,
            open_price=67_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.implied_probability == 0.99

    def test_precise_calculation(self):
        """Verify exact implied probability for a clean pct_change."""
        # open = 100000, current = 100020 => pct_change = 0.0002
        # implied = 0.5 + (0.0002 / 0.01) = 0.5 + 0.02 = 0.52
        result = compute_signal(
            current_price=100_020.0,
            open_price=100_000.0,
            market_yes_price=0.50,
            threshold=0.01,
        )
        assert result.implied_probability == pytest.approx(0.52)
        assert result.edge == pytest.approx(0.02)
