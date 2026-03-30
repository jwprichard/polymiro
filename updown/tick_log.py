"""Tick logger — records replayable tick streams in JSONL format.

Each tick is serialised as a single JSON line and appended to a daily
rotated file under ``data/updown_ticks_YYYY-MM-DD.jsonl``.

Activation is controlled by the ``UPDOWN_TICK_LOG_ENABLED`` config flag.
When disabled (the default), ``log_tick`` is a no-op with zero overhead —
the guard check is a single boolean comparison before any serialisation
or I/O occurs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from updown.types import TickContext
import config


class TickLogger:
    """Append-only JSONL writer for TickContext snapshots.

    Parameters
    ----------
    output_dir:
        Directory where daily JSONL files are written.  Defaults to
        ``config.DATA_DIR``.
    enabled:
        Override for the ``UPDOWN_TICK_LOG_ENABLED`` config flag.  When
        ``None`` (the default), reads the config value at construction
        time.
    """

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._enabled: bool = (
            enabled if enabled is not None else config.UPDOWN_TICK_LOG_ENABLED
        )
        self._output_dir: Path = output_dir or config.DATA_DIR
        # Track the currently open file handle and its date to detect rotation.
        self._current_date: Optional[str] = None
        self._file = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_tick(self, tick_context: TickContext) -> None:
        """Serialise *tick_context* and append one JSON line to the log.

        No-op when logging is disabled — the guard check is a single
        boolean comparison so there is zero overhead in production.
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

    def close(self) -> None:
        """Flush and close the underlying file handle, if any."""
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._current_date = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_file(self, date_str: str):
        """Return a file handle for *date_str*, rotating if necessary."""
        if self._current_date == date_str and self._file is not None:
            return self._file

        # Close previous day's handle if rotating.
        self.close()

        os.makedirs(self._output_dir, exist_ok=True)
        path = self._output_dir / f"updown_ticks_{date_str}.jsonl"
        self._file = open(path, "a", encoding="utf-8")
        self._current_date = date_str
        return self._file


# ----------------------------------------------------------------------
# Serialisation helper
# ----------------------------------------------------------------------

def _tick_to_record(ctx: TickContext) -> dict:
    """Extract replay-necessary fields from a TickContext into a plain dict."""
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
        "state": ctx.state.value,
        "entry_price": ctx.entry_price,
        "entry_time": ctx.entry_time,
        "entry_side": ctx.entry_side,
        "entry_size_usdc": ctx.entry_size_usdc,
    }
