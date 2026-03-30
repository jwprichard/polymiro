"""trading — trade execution layer.

Public surface
--------------
execute_trade(candidate: dict) -> dict
    Execute a single approved trade.  Behaviour is governed by config.DRY_MODE.

present_for_review(candidates: list[dict]) -> list[dict]
    Interactive review loop; returns approved trade records.

TradeExecutionError
    Raised only for unrecoverable configuration errors (e.g., CLI binary
    missing when DRY_MODE=False).
"""

from trading.trade_executor import (  # noqa: F401
    TradeExecutionError,
    execute_trade,
    present_for_review,
)

__all__ = [
    "execute_trade",
    "present_for_review",
    "TradeExecutionError",
]
