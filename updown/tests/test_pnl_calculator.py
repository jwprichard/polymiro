"""Tests for updown.pnl.calculator — P&L calculation engine."""

from __future__ import annotations

import pytest

from common import config
from updown.pnl.calculator import (
    _detect_source,
    _extract_entry_price,
    _normalise_outcome,
    _round_money,
    calculate_exit_pnl,
    calculate_pnl,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_trade(**overrides) -> dict:
    """Build a minimal valid trade record for calculate_pnl()."""
    base = {
        "trade_id": "t001",
        "market_id": "m001",
        "direction": "YES",
        "entry_price": 0.60,
        "amount_usdc": 10.0,
    }
    base.update(overrides)
    return base


def _make_exit_trade(**overrides) -> dict:
    """Build a minimal valid exit-trade record for calculate_exit_pnl()."""
    base = {
        "trade_id": "t002",
        "market_id": "m002",
        "outcome_bet": "YES",
        "entry_price": 0.50,
        "exit_price": 0.70,
        "amount_usdc": 10.0,
        "exit_reason": "take_profit",
        "hold_duration_s": 120,
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# _normalise_outcome
# ═══════════════════════════════════════════════════════════════════════════


class TestNormaliseOutcome:
    """Tests for _normalise_outcome()."""

    @pytest.mark.parametrize("raw,expected", [
        ("YES", "YES"),
        ("yes", "YES"),
        ("Yes", "YES"),
        ("  yes  ", "YES"),
        ("NO", "NO"),
        ("no", "NO"),
        ("No", "NO"),
        ("  no  ", "NO"),
    ])
    def test_valid_outcomes(self, raw: str, expected: str):
        assert _normalise_outcome(raw) == expected

    @pytest.mark.parametrize("raw", ["maybe", "up", "down", "", "   "])
    def test_invalid_outcomes_raise(self, raw: str):
        with pytest.raises(ValueError, match="Unrecognised outcome"):
            _normalise_outcome(raw)


# ═══════════════════════════════════════════════════════════════════════════
# _extract_entry_price
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractEntryPrice:
    """Tests for _extract_entry_price()."""

    def test_entry_price_field(self):
        assert _extract_entry_price({"entry_price": 0.55}) == 0.55

    def test_market_price_field(self):
        assert _extract_entry_price({"market_price": 0.45}) == 0.45

    def test_entry_price_takes_precedence(self):
        trade = {"entry_price": 0.60, "market_price": 0.40}
        assert _extract_entry_price(trade) == 0.60

    def test_string_value_converted_to_float(self):
        assert _extract_entry_price({"entry_price": "0.30"}) == 0.30

    def test_missing_both_raises_key_error(self):
        with pytest.raises(KeyError, match="entry_price.*market_price"):
            _extract_entry_price({"amount": 10})

    @pytest.mark.parametrize("price", [0.0, 1.0, -0.5, 1.5, 2.0])
    def test_out_of_range_raises_value_error(self, price: float):
        with pytest.raises(ValueError, match="open interval"):
            _extract_entry_price({"entry_price": price})


# ═══════════════════════════════════════════════════════════════════════════
# _detect_source
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectSource:
    """Tests for _detect_source()."""

    def test_explicit_source_field(self):
        assert _detect_source({"source": "custom_source"}) == "custom_source"

    def test_signal_key_implies_updown(self):
        assert _detect_source({"signal": {"direction": "UP"}}) == "updown"

    def test_token_id_key_implies_updown(self):
        assert _detect_source({"token_id": "tok_123"}) == "updown"

    def test_no_clues_defaults_to_research(self):
        assert _detect_source({"trade_id": "t1"}) == "research"

    def test_empty_source_string_defaults_to_inference(self):
        # Empty string is falsy, so falls through to inference
        assert _detect_source({"source": "", "signal": "x"}) == "updown"

    def test_source_field_overrides_signal(self):
        assert _detect_source({"source": "manual", "signal": "x"}) == "manual"


# ═══════════════════════════════════════════════════════════════════════════
# calculate_pnl — resolution settlement
# ═══════════════════════════════════════════════════════════════════════════


class TestCalculatePnl:
    """Tests for calculate_pnl() — binary market resolution."""

    def test_winning_yes_bet(self):
        """YES bet, market resolves YES -> profit."""
        trade = _make_trade(direction="YES", entry_price=0.60, amount_usdc=10.0)
        result = calculate_pnl(trade, "YES")

        assert result["outcome_bet"] == "YES"
        assert result["winning_outcome"] == "YES"

        # shares = 10 / 0.6 = 16.666667
        expected_shares = round(10.0 / 0.60, 6)
        assert result["shares"] == expected_shares

        # payout = shares * 1.0
        assert result["payout"] == expected_shares

        # gross_pnl = payout - 10
        expected_gross = round(expected_shares - 10.0, 6)
        assert result["gross_pnl"] == expected_gross

        # fee = PNL_FEE_RATE * gross_pnl
        expected_fee = round(config.PNL_FEE_RATE * expected_gross, 6)
        assert result["fee"] == expected_fee

        # net_pnl = gross - fee
        assert result["net_pnl"] == round(expected_gross - expected_fee, 6)
        assert result["settlement_type"] == "resolution"

    def test_losing_yes_bet(self):
        """YES bet, market resolves NO -> total loss."""
        trade = _make_trade(direction="YES", entry_price=0.60, amount_usdc=10.0)
        result = calculate_pnl(trade, "NO")

        assert result["outcome_bet"] == "YES"
        assert result["winning_outcome"] == "NO"
        assert result["payout"] == 0.0
        assert result["gross_pnl"] == -10.0
        assert result["fee"] == 0.0
        assert result["net_pnl"] == -10.0

    def test_winning_no_bet(self):
        """NO bet, market resolves NO -> profit."""
        trade = _make_trade(direction="NO", entry_price=0.40, amount_usdc=8.0)
        result = calculate_pnl(trade, "NO")

        expected_shares = round(8.0 / 0.40, 6)
        expected_gross = round(expected_shares - 8.0, 6)
        expected_fee = round(config.PNL_FEE_RATE * expected_gross, 6)

        assert result["outcome_bet"] == "NO"
        assert result["winning_outcome"] == "NO"
        assert result["shares"] == expected_shares
        assert result["gross_pnl"] == expected_gross
        assert result["fee"] == expected_fee
        assert result["net_pnl"] == round(expected_gross - expected_fee, 6)

    def test_losing_no_bet(self):
        """NO bet, market resolves YES -> total loss."""
        trade = _make_trade(direction="NO", entry_price=0.40, amount_usdc=8.0)
        result = calculate_pnl(trade, "YES")

        assert result["payout"] == 0.0
        assert result["gross_pnl"] == -8.0
        assert result["fee"] == 0.0
        assert result["net_pnl"] == -8.0

    def test_fee_uses_config_pnl_fee_rate(self, monkeypatch):
        """Verify that PNL_FEE_RATE from config is used for fee calculation."""
        monkeypatch.setattr(config, "PNL_FEE_RATE", 0.05)
        trade = _make_trade(entry_price=0.50, amount_usdc=10.0)
        result = calculate_pnl(trade, "YES")

        expected_shares = round(10.0 / 0.50, 6)
        expected_gross = round(expected_shares - 10.0, 6)
        expected_fee = round(0.05 * expected_gross, 6)
        assert result["fee"] == expected_fee

    def test_outcome_bet_field_accepted(self):
        """Trade with outcome_bet instead of direction."""
        trade = _make_trade(outcome_bet="YES")
        # Remove direction so outcome_bet is used
        trade.pop("direction", None)
        result = calculate_pnl(trade, "YES")
        assert result["outcome_bet"] == "YES"

    def test_market_price_field_accepted(self):
        """Trade with market_price instead of entry_price."""
        trade = _make_trade(market_price=0.55)
        trade.pop("entry_price", None)
        result = calculate_pnl(trade, "YES")
        assert result["entry_price"] == 0.55

    def test_case_insensitive_winning_outcome(self):
        """Winning outcome is normalised from lowercase."""
        trade = _make_trade(direction="yes")
        result = calculate_pnl(trade, "yes")
        assert result["winning_outcome"] == "YES"
        assert result["outcome_bet"] == "YES"

    def test_missing_trade_id_raises(self):
        trade = _make_trade()
        del trade["trade_id"]
        with pytest.raises(KeyError):
            calculate_pnl(trade, "YES")

    def test_missing_direction_raises(self):
        trade = _make_trade()
        del trade["direction"]
        with pytest.raises(KeyError, match="outcome_bet.*direction"):
            calculate_pnl(trade, "YES")

    def test_invalid_winning_outcome_raises(self):
        trade = _make_trade()
        with pytest.raises(ValueError, match="Unrecognised"):
            calculate_pnl(trade, "maybe")

    def test_negative_amount_raises(self):
        trade = _make_trade(amount_usdc=-5.0)
        with pytest.raises(ValueError, match="positive"):
            calculate_pnl(trade, "YES")

    def test_zero_amount_raises(self):
        trade = _make_trade(amount_usdc=0.0)
        with pytest.raises(ValueError, match="positive"):
            calculate_pnl(trade, "YES")

    def test_result_has_resolved_at_timestamp(self):
        trade = _make_trade()
        result = calculate_pnl(trade, "YES")
        assert "resolved_at" in result
        assert result["resolved_at"].endswith("Z")

    def test_source_detection_in_result(self):
        """A trade with token_id should be detected as updown source."""
        trade = _make_trade(token_id="tok_123")
        result = calculate_pnl(trade, "YES")
        assert result["source"] == "updown"


# ═══════════════════════════════════════════════════════════════════════════
# calculate_exit_pnl — exit settlement
# ═══════════════════════════════════════════════════════════════════════════


class TestCalculateExitPnl:
    """Tests for calculate_exit_pnl() — exit-based settlement."""

    def test_profitable_exit(self):
        """Buy at 0.50, exit at 0.70 -> profit after fees."""
        trade = _make_exit_trade(
            entry_price=0.50, exit_price=0.70, amount_usdc=10.0,
        )
        result = calculate_exit_pnl(trade)

        shares = round(10.0 / 0.50, 6)
        exit_value = round(shares * 0.70, 6)
        gross = round(exit_value - 10.0, 6)
        fee = round(config.PNL_FEE_RATE * gross, 6)
        net = round(gross - fee, 6)

        assert result["shares"] == shares
        assert result["payout"] == exit_value
        assert result["gross_pnl"] == gross
        assert result["fee"] == fee
        assert result["net_pnl"] == net
        assert result["settlement_type"] == "exit"
        assert result["exit_reason"] == "take_profit"
        assert result["hold_duration_s"] == 120

    def test_unprofitable_exit(self):
        """Buy at 0.60, exit at 0.40 -> loss, no fee."""
        trade = _make_exit_trade(
            entry_price=0.60, exit_price=0.40, amount_usdc=12.0,
        )
        result = calculate_exit_pnl(trade)

        shares = round(12.0 / 0.60, 6)
        exit_value = round(shares * 0.40, 6)
        gross = round(exit_value - 12.0, 6)

        assert gross < 0
        assert result["gross_pnl"] == gross
        assert result["fee"] == 0.0  # no fee on losses
        assert result["net_pnl"] == gross

    def test_breakeven_exit(self):
        """Exit at same price -> zero gross, zero fee."""
        trade = _make_exit_trade(entry_price=0.50, exit_price=0.50)
        result = calculate_exit_pnl(trade)
        assert result["gross_pnl"] == 0.0
        assert result["fee"] == 0.0
        assert result["net_pnl"] == 0.0

    def test_missing_exit_price_raises(self):
        trade = _make_exit_trade()
        del trade["exit_price"]
        with pytest.raises(KeyError, match="exit_price"):
            calculate_exit_pnl(trade)

    def test_outcome_from_outcome_field(self):
        """Exit trade with 'outcome' instead of 'outcome_bet'."""
        trade = _make_exit_trade(outcome="no")
        del trade["outcome_bet"]
        result = calculate_exit_pnl(trade)
        assert result["outcome_bet"] == "NO"

    def test_missing_outcome_raises(self):
        trade = _make_exit_trade()
        del trade["outcome_bet"]
        with pytest.raises(KeyError, match="outcome_bet.*outcome"):
            calculate_exit_pnl(trade)

    def test_exit_fee_uses_config_rate(self, monkeypatch):
        monkeypatch.setattr(config, "PNL_FEE_RATE", 0.10)
        trade = _make_exit_trade(entry_price=0.50, exit_price=0.80, amount_usdc=10.0)
        result = calculate_exit_pnl(trade)

        shares = round(10.0 / 0.50, 6)
        exit_value = round(shares * 0.80, 6)
        gross = round(exit_value - 10.0, 6)
        expected_fee = round(0.10 * gross, 6)
        assert result["fee"] == expected_fee
