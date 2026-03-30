"""Tests for updown/tick_log.py — TickLogger, TradeEventLogger, _DailyRotatingLogger."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from updown.tick_log import TickLogger, TradeEventLogger, _DailyRotatingLogger, _tick_to_record
from updown.tests.conftest import make_tick_context


# ═══════════════════════════════════════════════════════════════════════════
# _tick_to_record
# ═══════════════════════════════════════════════════════════════════════════


class TestTickToRecord:
    """Unit tests for the _tick_to_record serialisation helper."""

    def test_expected_keys(self) -> None:
        ctx = make_tick_context()
        record = _tick_to_record(ctx)
        expected_keys = {
            "timestamp_ms",
            "price",
            "open_price",
            "yes_price",
            "no_price",
            "price_age_ms",
            "market_id",
            "token_id",
            "expiry_time",
        }
        assert set(record.keys()) == expected_keys

    def test_values_match_context(self) -> None:
        ctx = make_tick_context(
            tick_price=68_000.0,
            tick_timestamp_ms=1_700_000_100_000,
            open_price=67_500.0,
            yes_price=0.55,
            no_price=0.45,
            price_age_ms=200,
            market_id="0xabc",
            token_id="tok_123",
            expiry_time=1_700_000_400.0,
        )
        record = _tick_to_record(ctx)
        assert record["timestamp_ms"] == 1_700_000_100_000
        assert record["price"] == 68_000.0
        assert record["open_price"] == 67_500.0
        assert record["yes_price"] == 0.55
        assert record["no_price"] == 0.45
        assert record["price_age_ms"] == 200
        assert record["market_id"] == "0xabc"
        assert record["token_id"] == "tok_123"
        assert record["expiry_time"] == 1_700_000_400.0

    def test_no_extra_keys(self) -> None:
        """_tick_to_record must not leak position or strategy fields."""
        ctx = make_tick_context(entry_price=0.55, entry_side="YES")
        record = _tick_to_record(ctx)
        assert "entry_price" not in record
        assert "entry_side" not in record
        assert "strategy_config" not in record
        assert "state" not in record


# ═══════════════════════════════════════════════════════════════════════════
# TickLogger
# ═══════════════════════════════════════════════════════════════════════════


class TestTickLogger:
    """Tests for TickLogger.log_tick — JSONL writing, no-op, rotation."""

    def test_writes_jsonl_to_tmp_path(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=True)
        ctx = make_tick_context(tick_timestamp_ms=1_700_000_000_000)
        logger.log_tick(ctx)
        logger.close()

        date_str = datetime.fromtimestamp(
            1_700_000_000_000 / 1000.0, tz=timezone.utc,
        ).strftime("%Y-%m-%d")
        log_file = tmp_path / f"updown_ticks_{date_str}.jsonl"
        assert log_file.exists()

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["timestamp_ms"] == 1_700_000_000_000
        assert record["price"] == 67_000.0

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=False)
        ctx = make_tick_context()
        logger.log_tick(ctx)
        logger.close()

        # No files should have been created.
        assert list(tmp_path.iterdir()) == []

    def test_multiple_ticks_same_day(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=True)
        # Two ticks on the same day (1 second apart)
        ts1 = 1_700_000_000_000
        ts2 = 1_700_000_001_000
        logger.log_tick(make_tick_context(tick_timestamp_ms=ts1, tick_price=67_000.0))
        logger.log_tick(make_tick_context(tick_timestamp_ms=ts2, tick_price=67_100.0))
        logger.close()

        date_str = datetime.fromtimestamp(ts1 / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        log_file = tmp_path / f"updown_ticks_{date_str}.jsonl"
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["price"] == 67_000.0
        assert json.loads(lines[1])["price"] == 67_100.0

    def test_daily_rotation_creates_new_file(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=True)

        # Day 1: 2023-11-14 12:00:00 UTC
        ts_day1 = 1_699_963_200_000
        # Day 2: 2023-11-15 12:00:00 UTC
        ts_day2 = 1_700_049_600_000

        logger.log_tick(make_tick_context(tick_timestamp_ms=ts_day1))
        logger.log_tick(make_tick_context(tick_timestamp_ms=ts_day2))
        logger.close()

        date1 = datetime.fromtimestamp(ts_day1 / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        date2 = datetime.fromtimestamp(ts_day2 / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

        # Day 1 file should have been compressed
        assert (tmp_path / f"updown_ticks_{date1}.jsonl.gz").exists()
        assert not (tmp_path / f"updown_ticks_{date1}.jsonl").exists()

        # Day 2 file should be plain JSONL (still open)
        assert (tmp_path / f"updown_ticks_{date2}.jsonl").exists()


# ═══════════════════════════════════════════════════════════════════════════
# TradeEventLogger
# ═══════════════════════════════════════════════════════════════════════════


class TestTradeEventLogger:
    """Tests for TradeEventLogger.log_event — JSONL writing, no-op, timestamps."""

    def test_writes_jsonl_to_tmp_path(self, tmp_path: Path) -> None:
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        ts_ms = 1_700_000_000_000
        record = {
            "type": "entry",
            "exchange_timestamp_ms": ts_ms,
            "side": "YES",
            "price": 0.55,
        }
        logger.log_event(record)
        logger.close()

        date_str = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        log_file = tmp_path / f"updown_events_{date_str}.jsonl"
        assert log_file.exists()

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["type"] == "entry"
        assert parsed["side"] == "YES"

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        logger = TradeEventLogger(output_dir=tmp_path, enabled=False)
        logger.log_event({"type": "entry", "exchange_timestamp_ms": 1_700_000_000_000})
        logger.close()

        assert list(tmp_path.iterdir()) == []

    def test_uses_exchange_timestamp_ms(self, tmp_path: Path) -> None:
        """When exchange_timestamp_ms is present, the date is derived from it."""
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        ts_ms = 1_700_000_000_000
        logger.log_event({"exchange_timestamp_ms": ts_ms})
        logger.close()

        date_str = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        assert (tmp_path / f"updown_events_{date_str}.jsonl").exists()

    def test_falls_back_to_timestamp_ms(self, tmp_path: Path) -> None:
        """When exchange_timestamp_ms is absent, falls back to timestamp_ms."""
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        ts_ms = 1_700_000_000_000
        logger.log_event({"timestamp_ms": ts_ms})
        logger.close()

        date_str = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        assert (tmp_path / f"updown_events_{date_str}.jsonl").exists()

    def test_falls_back_to_utcnow_when_no_timestamp(self, tmp_path: Path) -> None:
        """When neither timestamp field is present, uses current UTC date."""
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        logger.log_event({"type": "test"})
        logger.close()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert (tmp_path / f"updown_events_{today}.jsonl").exists()

    def test_record_without_exchange_timestamp_ms(self, tmp_path: Path) -> None:
        """A record with only timestamp_ms=0 and no exchange_timestamp_ms
        should fall back to current UTC date (since ts_ms=0 is falsy)."""
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        logger.log_event({"type": "test", "timestamp_ms": 0})
        logger.close()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert (tmp_path / f"updown_events_{today}.jsonl").exists()


# ═══════════════════════════════════════════════════════════════════════════
# _DailyRotatingLogger._compress_previous
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressPrevious:
    """Tests for _DailyRotatingLogger._compress_previous."""

    def test_creates_gz_and_removes_original(self, tmp_path: Path) -> None:
        logger = _DailyRotatingLogger(prefix="test_log", output_dir=tmp_path, enabled=True)

        # Write a fake JSONL file for "2023-11-14"
        src = tmp_path / "test_log_2023-11-14.jsonl"
        content = '{"foo":"bar"}\n{"baz":42}\n'
        src.write_text(content)

        logger._compress_previous("2023-11-14")

        gz_path = tmp_path / "test_log_2023-11-14.jsonl.gz"
        assert gz_path.exists()
        assert not src.exists()

        # Verify the gzip contents match
        with gzip.open(gz_path, "rb") as f:
            decompressed = f.read().decode("utf-8")
        assert decompressed == content

    def test_noop_when_source_missing(self, tmp_path: Path) -> None:
        """_compress_previous should silently skip if the source file doesn't exist."""
        logger = _DailyRotatingLogger(prefix="test_log", output_dir=tmp_path, enabled=True)
        # Should not raise
        logger._compress_previous("2099-01-01")
        # No .gz file created
        assert not (tmp_path / "test_log_2099-01-01.jsonl.gz").exists()

    def test_compression_preserves_multiline_content(self, tmp_path: Path) -> None:
        logger = _DailyRotatingLogger(prefix="events", output_dir=tmp_path, enabled=True)
        src = tmp_path / "events_2023-11-14.jsonl"
        lines = [json.dumps({"i": i}) for i in range(100)]
        content = "\n".join(lines) + "\n"
        src.write_text(content)

        logger._compress_previous("2023-11-14")

        gz_path = tmp_path / "events_2023-11-14.jsonl.gz"
        with gzip.open(gz_path, "rb") as f:
            decompressed = f.read().decode("utf-8")
        assert decompressed == content


# ═══════════════════════════════════════════════════════════════════════════
# close()
# ═══════════════════════════════════════════════════════════════════════════


class TestClose:
    """Tests for the close() method — file handle release."""

    def test_close_releases_file_handle(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=True)
        ctx = make_tick_context(tick_timestamp_ms=1_700_000_000_000)
        logger.log_tick(ctx)

        assert logger._file is not None
        assert not logger._file.closed

        logger.close()
        assert logger._file is None
        assert logger._current_date is None

    def test_close_idempotent(self, tmp_path: Path) -> None:
        """Calling close() multiple times should not raise."""
        logger = TickLogger(output_dir=tmp_path, enabled=True)
        logger.log_tick(make_tick_context())
        logger.close()
        logger.close()  # second call should be a no-op
        assert logger._file is None

    def test_close_on_never_opened_logger(self, tmp_path: Path) -> None:
        """Closing a logger that never wrote anything should be fine."""
        logger = TickLogger(output_dir=tmp_path, enabled=True)
        logger.close()
        assert logger._file is None

    def test_trade_event_logger_close(self, tmp_path: Path) -> None:
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)
        logger.log_event({"exchange_timestamp_ms": 1_700_000_000_000, "type": "entry"})
        assert logger._file is not None
        logger.close()
        assert logger._file is None


# ═══════════════════════════════════════════════════════════════════════════
# Integration: rotation + compression end-to-end
# ═══════════════════════════════════════════════════════════════════════════


class TestRotationIntegration:
    """End-to-end test: write across day boundary, verify compression + new file."""

    def test_tick_logger_rotation_compresses_and_continues(self, tmp_path: Path) -> None:
        logger = TickLogger(output_dir=tmp_path, enabled=True)

        # 2023-11-14 23:59:59 UTC
        ts_before = 1_700_006_399_000
        # 2023-11-15 00:00:01 UTC
        ts_after = 1_700_006_401_000

        logger.log_tick(make_tick_context(tick_timestamp_ms=ts_before, tick_price=67_000.0))
        logger.log_tick(make_tick_context(tick_timestamp_ms=ts_after, tick_price=67_100.0))
        logger.close()

        date_before = datetime.fromtimestamp(ts_before / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        date_after = datetime.fromtimestamp(ts_after / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

        # Old day compressed
        gz_path = tmp_path / f"updown_ticks_{date_before}.jsonl.gz"
        assert gz_path.exists()

        with gzip.open(gz_path, "rb") as f:
            old_record = json.loads(f.read().decode("utf-8").strip())
        assert old_record["price"] == 67_000.0

        # New day still plain JSONL
        new_file = tmp_path / f"updown_ticks_{date_after}.jsonl"
        assert new_file.exists()
        new_record = json.loads(new_file.read_text().strip())
        assert new_record["price"] == 67_100.0

    def test_trade_event_logger_rotation(self, tmp_path: Path) -> None:
        logger = TradeEventLogger(output_dir=tmp_path, enabled=True)

        ts_day1 = 1_699_963_200_000  # 2023-11-14 12:00 UTC
        ts_day2 = 1_700_049_600_000  # 2023-11-15 12:00 UTC

        logger.log_event({"exchange_timestamp_ms": ts_day1, "type": "entry"})
        logger.log_event({"exchange_timestamp_ms": ts_day2, "type": "exit"})
        logger.close()

        date1 = datetime.fromtimestamp(ts_day1 / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        date2 = datetime.fromtimestamp(ts_day2 / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

        assert (tmp_path / f"updown_events_{date1}.jsonl.gz").exists()
        assert (tmp_path / f"updown_events_{date2}.jsonl").exists()
