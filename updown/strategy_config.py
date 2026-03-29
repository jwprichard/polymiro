"""updown/strategy_config.py — Typed strategy configuration with YAML loader.

Defines dataclasses that mirror the exit_rules (and future) sections of
strategy.yml, plus a fail-fast loader that validates every field on startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TakeProfitConfig:
    enabled: bool
    target_delta: float


@dataclass(frozen=True)
class StopLossConfig:
    enabled: bool
    max_loss_delta: float


@dataclass(frozen=True)
class TimeExitConfig:
    enabled: bool
    max_hold_seconds: float


@dataclass(frozen=True)
class ExitRulesConfig:
    take_profit: TakeProfitConfig
    stop_loss: StopLossConfig
    time_exit: TimeExitConfig
    allow_reentry: bool


@dataclass(frozen=True)
class StrategyConfig:
    exit_rules: ExitRulesConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Return mapping[key] or raise ValueError with a clear message."""
    if key not in mapping:
        raise ValueError(f"Missing required field '{key}' in {context}")
    return mapping[key]


def _require_positive_float(value: Any, field_name: str) -> float:
    """Validate that *value* is a positive number and return it as float."""
    try:
        fval = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"'{field_name}' must be a positive number, got {value!r}"
        )
    if fval <= 0:
        raise ValueError(
            f"'{field_name}' must be positive, got {fval}"
        )
    return fval


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            f"'{field_name}' must be a boolean, got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_strategy_config(path: Path) -> StrategyConfig:
    """Parse *path* (a YAML file) into a validated :class:`StrategyConfig`.

    Raises :class:`ValueError` on any missing or invalid field so the
    process fails fast at startup rather than mid-trade.
    """
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ValueError(f"Strategy file not found: {path}")
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}")

    if not isinstance(raw, dict):
        raise ValueError(f"Expected top-level mapping in {path}, got {type(raw).__name__}")

    # -- exit_rules (required) ---------------------------------------------
    exit_raw = _require(raw, "exit_rules", "strategy.yml")
    if not isinstance(exit_raw, dict):
        raise ValueError("'exit_rules' must be a mapping")

    # take_profit
    tp_raw = _require(exit_raw, "take_profit", "exit_rules")
    if not isinstance(tp_raw, dict):
        raise ValueError("'exit_rules.take_profit' must be a mapping")
    take_profit = TakeProfitConfig(
        enabled=_require_bool(
            _require(tp_raw, "enabled", "exit_rules.take_profit"),
            "exit_rules.take_profit.enabled",
        ),
        target_delta=_require_positive_float(
            _require(tp_raw, "target_delta", "exit_rules.take_profit"),
            "exit_rules.take_profit.target_delta",
        ),
    )

    # stop_loss
    sl_raw = _require(exit_raw, "stop_loss", "exit_rules")
    if not isinstance(sl_raw, dict):
        raise ValueError("'exit_rules.stop_loss' must be a mapping")
    stop_loss = StopLossConfig(
        enabled=_require_bool(
            _require(sl_raw, "enabled", "exit_rules.stop_loss"),
            "exit_rules.stop_loss.enabled",
        ),
        max_loss_delta=_require_positive_float(
            _require(sl_raw, "max_loss_delta", "exit_rules.stop_loss"),
            "exit_rules.stop_loss.max_loss_delta",
        ),
    )

    # time_exit
    te_raw = _require(exit_raw, "time_exit", "exit_rules")
    if not isinstance(te_raw, dict):
        raise ValueError("'exit_rules.time_exit' must be a mapping")
    time_exit = TimeExitConfig(
        enabled=_require_bool(
            _require(te_raw, "enabled", "exit_rules.time_exit"),
            "exit_rules.time_exit.enabled",
        ),
        max_hold_seconds=_require_positive_float(
            _require(te_raw, "max_hold_seconds", "exit_rules.time_exit"),
            "exit_rules.time_exit.max_hold_seconds",
        ),
    )

    # allow_reentry
    allow_reentry = _require_bool(
        _require(exit_raw, "allow_reentry", "exit_rules"),
        "exit_rules.allow_reentry",
    )

    exit_rules = ExitRulesConfig(
        take_profit=take_profit,
        stop_loss=stop_loss,
        time_exit=time_exit,
        allow_reentry=allow_reentry,
    )

    return StrategyConfig(exit_rules=exit_rules)
