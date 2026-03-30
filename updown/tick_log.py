"""Tick & trade-event loggers — daily-rotated JSONL with gzip compression.

**TickLogger** records price-only market data ticks to
``data/updown_ticks_YYYY-MM-DD.jsonl`` for backtesting via ``replay.py``.

**TradeEventLogger** records trade entry/exit events to
``data/updown_events_YYYY-MM-DD.jsonl`` for audit and P&L review.

Both loggers gzip the previous day's file on daily rotation.  Activation
is controlled by the ``UPDOWN_TICK_LOG_ENABLED`` config flag (applies to
both loggers).
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from updown.types import TickContext
from common import config


# ----------------------------------------------------------------------
# Shared rotation / compression mixin
# ----------------------------------------------------------------------

class _DailyRotatingLogger:
    """Base class for daily-rotated, gzip-compressed JSONL writers."""

    def __init__(
        self,
        prefix: str,
        output_dir: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._enabled: bool = (
            enabled if enabled is not None else config.UPDOWN_TICK_LOG_ENABLED
        )
        self._prefix = prefix
        self._output_dir: Path = output_dir or config.UPDOWN_DATA_DIR
        self._current_date: Optional[str] = None
        self._file = None

    def close(self) -> None:
        """Flush and close the underlying file handle, if any."""
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._current_date = None

    def _get_file(self, date_str: str):
        """Return a file handle for *date_str*, rotating if necessary."""
        if self._current_date == date_str and self._file is not None:
            return self._file

        prev_date = self._current_date
        self.close()

        if prev_date is not None:
            self._compress_previous(prev_date)

        os.makedirs(self._output_dir, exist_ok=True)
        path = self._output_dir / f"{self._prefix}_{date_str}.jsonl"
        self._file = open(path, "a", encoding="utf-8")
        self._current_date = date_str
        return self._file

    def _compress_previous(self, date_str: str) -> None:
        """Gzip a previous day's JSONL file and remove the original."""
        src = self._output_dir / f"{self._prefix}_{date_str}.jsonl"
        if not src.exists():
            return
        dst = src.with_suffix(".jsonl.gz")
        try:
            with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
                while chunk := f_in.read(1 << 20):
                    f_out.write(chunk)
            src.unlink()
        except OSError:
            if dst.exists() and src.exists():
                dst.unlink()


# ----------------------------------------------------------------------
# TickLogger — price-only market data
# ----------------------------------------------------------------------

class TickLogger(_DailyRotatingLogger):
    """Append-only JSONL writer for price-only market data ticks."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        super().__init__(prefix="updown_ticks", output_dir=output_dir, enabled=enabled)

    def log_tick(self, tick_context: TickContext) -> None:
        """Serialise *tick_context* and append one JSON line to the log.

        No-op when logging is disabled.
        """
        if not self._enabled:
            return

        record = _tick_to_record(tick_context)
        line = json.dumps(record, separators=(",", ":")) + "\n"

        date_str = datetime.fromtimestamp(
            tick_context.tick_timestamp_ms / 1000.0, tz=timezone.utc,
        ).strftime("%Y-%m-%d")

        fh = self._get_file(date_str)
        fh.write(line)
        fh.flush()


# ----------------------------------------------------------------------
# TradeEventLogger — entry/exit audit trail
# ----------------------------------------------------------------------

class TradeEventLogger(_DailyRotatingLogger):
    """Append-only JSONL writer for trade entry/exit events."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        super().__init__(prefix="updown_events", output_dir=output_dir, enabled=enabled)

    def log_event(self, record: dict) -> None:
        """Append a trade event record as one JSON line.

        No-op when logging is disabled.
        """
        if not self._enabled:
            return

        line = json.dumps(record, separators=(",", ":")) + "\n"

        ts_ms = record.get("exchange_timestamp_ms") or record.get("timestamp_ms", 0)
        if ts_ms:
            date_str = datetime.fromtimestamp(
                ts_ms / 1000.0, tz=timezone.utc,
            ).strftime("%Y-%m-%d")
        else:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        fh = self._get_file(date_str)
        fh.write(line)
        fh.flush()


# ----------------------------------------------------------------------
# Serialisation helper
# ----------------------------------------------------------------------

def _tick_to_record(ctx: TickContext) -> dict:
    """Extract price-only market data from a TickContext."""
    return {
        "timestamp_ms": ctx.tick_timestamp_ms,
        "price": ctx.tick_price,
        "open_price": ctx.open_price,
        "yes_price": ctx.yes_price,
        "no_price": ctx.no_price,
        "price_age_ms": ctx.price_age_ms,
        "market_id": ctx.market_id,
        "token_id": ctx.token_id,
        "expiry_time": ctx.expiry_time,
    }
