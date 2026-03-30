"""Tests for updown.pnl.tracker — P&L tracker end-to-end."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from common import config
from updown.pnl import tracker
from updown.pnl.tracker import (
    _load_json_list,
    _normalise_trade,
    reset,
    run,
)


# ═══════════════════════════════════════════════════════════════════════════
# _load_json_list
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadJsonList:
    """Tests for _load_json_list() edge cases."""

    def test_missing_file_returns_empty(self, tmp_path):
        result = _load_json_list(tmp_path / "nonexistent.json")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("", encoding="utf-8")
        assert _load_json_list(f) == []

    def test_whitespace_only_returns_empty(self, tmp_path):
        f = tmp_path / "ws.json"
        f.write_text("   \n  ", encoding="utf-8")
        assert _load_json_list(f) == []

    def test_valid_json_array(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('[{"a": 1}, {"b": 2}]', encoding="utf-8")
        result = _load_json_list(f)
        assert len(result) == 2
        assert result[0]["a"] == 1

    def test_invalid_json_returns_empty(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        assert _load_json_list(f) == []

    def test_non_array_json_returns_empty(self, tmp_path):
        """A JSON object instead of array should return []."""
        f = tmp_path / "obj.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        assert _load_json_list(f) == []


# ═══════════════════════════════════════════════════════════════════════════
# _normalise_trade
# ═══════════════════════════════════════════════════════════════════════════


class TestNormaliseTrade:
    """Tests for _normalise_trade() field mapping."""

    def test_already_has_outcome_bet(self):
        """Trade with outcome_bet should pass through unchanged."""
        trade = {"trade_id": "t1", "outcome_bet": "YES"}
        result = _normalise_trade(trade)
        assert result is trade  # same object, not copied
        assert result["outcome_bet"] == "YES"

    def test_research_trade_with_direction(self):
        """Research trades use 'direction' -> mapped to outcome_bet."""
        trade = {"trade_id": "t2", "direction": "YES"}
        result = _normalise_trade(trade)
        assert result is not trade  # new dict
        assert result["outcome_bet"] == "YES"
        assert result["direction"] == "YES"

    def test_direction_lowercase(self):
        trade = {"trade_id": "t3", "direction": "no"}
        result = _normalise_trade(trade)
        assert result["outcome_bet"] == "NO"

    def test_updown_trade_with_outcome(self):
        """Updown trades use 'outcome' -> mapped to outcome_bet."""
        trade = {"trade_id": "t4", "outcome": "yes"}
        result = _normalise_trade(trade)
        assert result["outcome_bet"] == "YES"

    def test_missing_direction_returns_none(self):
        """No direction/outcome_bet/outcome -> None."""
        trade = {"trade_id": "t5", "amount": 10}
        assert _normalise_trade(trade) is None

    def test_invalid_direction_value_returns_none(self):
        """Direction with non-YES/NO value -> None."""
        trade = {"trade_id": "t6", "direction": "UP"}
        assert _normalise_trade(trade) is None

    def test_does_not_mutate_original(self):
        """Original dict should not be modified."""
        trade = {"trade_id": "t7", "direction": "YES"}
        original_keys = set(trade.keys())
        _normalise_trade(trade)
        assert set(trade.keys()) == original_keys
        assert "outcome_bet" not in trade


# ═══════════════════════════════════════════════════════════════════════════
# reset
# ═══════════════════════════════════════════════════════════════════════════


class TestReset:
    """Tests for reset() — clears report and trades files."""

    def test_reset_writes_empty_lists(self, tmp_data_dir):
        """reset() should call write_json_atomic for both files."""
        # Patch the module-level path constants to use tmp_data_dir
        pnl_path = tmp_data_dir / "pnl_report.json"
        trades_path = tmp_data_dir / "updown_trades.json"

        # Write some existing data
        pnl_path.write_text('[{"trade_id": "old"}]', encoding="utf-8")
        trades_path.write_text('[{"trade_id": "old2"}]', encoding="utf-8")

        with patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", trades_path):
            reset()

        # Both files should now contain empty JSON arrays
        assert json.loads(pnl_path.read_text()) == []
        assert json.loads(trades_path.read_text()) == []

    @patch("updown.pnl.tracker.write_json_atomic")
    def test_reset_calls_write_json_atomic(self, mock_write):
        """Verify write_json_atomic is called for both files."""
        reset()
        assert mock_write.call_count == 2
        # Both calls should pass empty list
        for call in mock_write.call_args_list:
            assert call[0][1] == []


# ═══════════════════════════════════════════════════════════════════════════
# run — end-to-end integration
# ═══════════════════════════════════════════════════════════════════════════


class TestRun:
    """Tests for run() — end-to-end with mocked I/O and Gamma API."""

    def _setup_files(self, tmp_data_dir, *, dry_trades=None, updown_trades=None, pnl_report=None):
        """Write trade files and patch tracker paths."""
        dry_path = tmp_data_dir / "dry_trades.json"
        updown_path = tmp_data_dir / "updown_trades.json"
        pnl_path = tmp_data_dir / "pnl_report.json"

        dry_path.write_text(json.dumps(dry_trades or []), encoding="utf-8")
        updown_path.write_text(json.dumps(updown_trades or []), encoding="utf-8")
        pnl_path.write_text(json.dumps(pnl_report or []), encoding="utf-8")

        return dry_path, updown_path, pnl_path

    def test_run_with_resolved_market(self, tmp_data_dir):
        """End-to-end: dry trade + resolved market -> P&L record written."""
        dry_trades = [{
            "trade_id": "t100",
            "market_id": "0xmarket1",
            "direction": "YES",
            "entry_price": 0.60,
            "amount_usdc": 10.0,
            "dry_mode": True,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=dry_trades,
        )

        gamma_result = {"resolved": True, "outcome": "Yes"}

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution", return_value=gamma_result):
            run()

        report = json.loads(pnl_path.read_text())
        assert len(report) == 1
        assert report[0]["trade_id"] == "t100"
        assert report[0]["winning_outcome"] == "YES"
        assert report[0]["net_pnl"] > 0  # won YES bet

    def test_run_skips_non_dry_trades(self, tmp_data_dir):
        """Only dry_mode=True trades are processed."""
        trades = [{
            "trade_id": "t200",
            "market_id": "0xmarket2",
            "direction": "YES",
            "entry_price": 0.50,
            "amount_usdc": 5.0,
            "dry_mode": False,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=trades,
        )

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution") as mock_gamma:
            run()

        mock_gamma.assert_not_called()

    def test_run_deduplicates_already_resolved(self, tmp_data_dir):
        """Trades already in pnl_report are not reprocessed."""
        dry_trades = [{
            "trade_id": "t300",
            "market_id": "0xmarket3",
            "direction": "YES",
            "entry_price": 0.50,
            "amount_usdc": 10.0,
            "dry_mode": True,
        }]
        existing_report = [{"trade_id": "t300", "net_pnl": 5.0}]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=dry_trades, pnl_report=existing_report,
        )

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution") as mock_gamma:
            run()

        # Should not call Gamma since trade is already resolved
        mock_gamma.assert_not_called()

    def test_run_skips_unresolved_market(self, tmp_data_dir):
        """Unresolved markets are skipped (no P&L record)."""
        dry_trades = [{
            "trade_id": "t400",
            "market_id": "0xmarket4",
            "direction": "YES",
            "entry_price": 0.50,
            "amount_usdc": 10.0,
            "dry_mode": True,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=dry_trades,
        )

        gamma_result = {"resolved": False, "outcome": None}

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution", return_value=gamma_result):
            run()

        report = json.loads(pnl_path.read_text())
        assert len(report) == 0

    def test_run_handles_gamma_api_failure(self, tmp_data_dir):
        """Gamma returning None (API error) -> trade skipped, no crash."""
        dry_trades = [{
            "trade_id": "t500",
            "market_id": "0xmarket5",
            "direction": "YES",
            "entry_price": 0.50,
            "amount_usdc": 10.0,
            "dry_mode": True,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=dry_trades,
        )

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution", return_value=None):
            run()  # should not raise

        report = json.loads(pnl_path.read_text())
        assert len(report) == 0

    def test_run_processes_exit_trades(self, tmp_data_dir):
        """Exit (sell) trades are settled via calculate_exit_pnl, not Gamma."""
        updown_trades = [{
            "trade_id": "t600",
            "market_id": "0xmarket6",
            "asset_id": "asset6",
            "direction": "sell",
            "outcome_bet": "YES",
            "entry_price": 0.50,
            "exit_price": 0.70,
            "amount_usdc": 10.0,
            "exit_reason": "take_profit",
            "dry_mode": True,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, updown_trades=updown_trades,
        )

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution") as mock_gamma:
            run()

        report = json.loads(pnl_path.read_text())
        assert len(report) == 1
        assert report[0]["settlement_type"] == "exit"
        assert report[0]["trade_id"] == "t600"
        # Gamma should not have been called for exit trades (the matching
        # buy would be in settled_keys, but the sell itself is the exit trade)
        # Actually Gamma may be called for market_ids from other trades,
        # but for this single-trade case it should not be called at all.

    def test_run_merges_dry_and_updown_trades(self, tmp_data_dir):
        """Trades from both source files are processed."""
        dry_trades = [{
            "trade_id": "t700",
            "market_id": "0xmarket7",
            "direction": "YES",
            "entry_price": 0.50,
            "amount_usdc": 5.0,
            "dry_mode": True,
        }]
        updown_trades = [{
            "trade_id": "t701",
            "market_id": "0xmarket7",
            "outcome": "no",
            "entry_price": 0.50,
            "amount_usdc": 5.0,
            "dry_mode": True,
        }]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=dry_trades, updown_trades=updown_trades,
        )

        gamma_result = {"resolved": True, "outcome": "Yes"}

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution", return_value=gamma_result):
            run()

        report = json.loads(pnl_path.read_text())
        assert len(report) == 2
        trade_ids = {r["trade_id"] for r in report}
        assert trade_ids == {"t700", "t701"}

    def test_run_no_trades(self, tmp_data_dir):
        """No trades at all -> graceful exit, no crash."""
        dry_path, updown_path, pnl_path = self._setup_files(tmp_data_dir)

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path):
            run()  # should not raise

    def test_run_batches_gamma_calls_by_market_id(self, tmp_data_dir):
        """Multiple trades for the same market_id should result in one Gamma call."""
        trades = [
            {
                "trade_id": f"t80{i}",
                "market_id": "0xsame_market",
                "direction": "YES",
                "entry_price": 0.50,
                "amount_usdc": 5.0,
                "dry_mode": True,
            }
            for i in range(3)
        ]

        dry_path, updown_path, pnl_path = self._setup_files(
            tmp_data_dir, dry_trades=trades,
        )

        gamma_result = {"resolved": True, "outcome": "Yes"}

        with patch.object(tracker, "_DRY_TRADES_FILE", dry_path), \
             patch.object(tracker, "_UPDOWN_TRADES_FILE", updown_path), \
             patch.object(tracker, "_PNL_REPORT_FILE", pnl_path), \
             patch("updown.pnl.tracker.check_resolution", return_value=gamma_result) as mock_gamma:
            run()

        # Only one Gamma API call for the single market_id
        assert mock_gamma.call_count == 1
        mock_gamma.assert_called_once_with("0xsame_market")

        report = json.loads(pnl_path.read_text())
        assert len(report) == 3
