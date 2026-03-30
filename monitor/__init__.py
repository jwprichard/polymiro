"""monitor — Portfolio monitoring for open Polymarket positions.

Public API
----------
run_monitor(risk_profile=None) -> dict
    Scan all open positions, fetch current prices, compute edges, and write
    data/monitor_report.json.  Returns the report dict.

MonitorError
    Raised on unrecoverable I/O errors.  Per-position fetch failures are
    recorded inside the report and do not raise.
"""

# Deferred import avoids the double-import RuntimeWarning produced by
# "python -m monitor.portfolio_monitor" when __init__.py imports the same
# module before runpy executes it.
from __future__ import annotations


def __getattr__(name: str):
    if name in ("run_monitor", "MonitorError"):
        from monitor import portfolio_monitor as _pm  # noqa: PLC0415
        return getattr(_pm, name)
    raise AttributeError(f"module 'monitor' has no attribute {name!r}")


__all__ = ["run_monitor", "MonitorError"]
