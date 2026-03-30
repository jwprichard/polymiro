"""updown/strategy_config.py — Typed strategy configuration with YAML loader.

Defines frozen dataclasses that mirror every active section of the strategy
YAML (e.g. ``updown/strategies/btc_lag_arbitrage.yml``), plus a fail-fast
loader that validates every field on startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Validation helpers
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


def _require_non_negative_float(value: Any, field_name: str) -> float:
    """Validate that *value* is a non-negative number and return it as float."""
    try:
        fval = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"'{field_name}' must be a non-negative number, got {value!r}"
        )
    if fval < 0:
        raise ValueError(
            f"'{field_name}' must be non-negative, got {fval}"
        )
    return fval


def _require_positive_int(value: Any, field_name: str) -> int:
    """Validate that *value* is a positive integer and return it as int."""
    try:
        ival = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"'{field_name}' must be a positive integer, got {value!r}"
        )
    if ival <= 0:
        raise ValueError(
            f"'{field_name}' must be positive, got {ival}"
        )
    return ival


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            f"'{field_name}' must be a boolean, got {value!r}"
        )
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"'{field_name}' must be a string, got {value!r}"
        )
    return value


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{context}' must be a mapping, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Dataclasses — Strategy metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyMeta:
    name: str
    type: str
    version: int
    description: str


# ---------------------------------------------------------------------------
# Dataclasses — Signals (with ThresholdsConfig sub-config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdsConfig:
    momentum_threshold: float
    confirmation_ticks: int


@dataclass(frozen=True)
class SignalsConfig:
    type: str
    lookback_seconds: int
    smoothing: str
    thresholds: ThresholdsConfig

    # Convenience accessors so existing code that reads
    # ``signals.momentum_threshold`` / ``signals.confirmation_ticks``
    # continues to work without changes.
    @property
    def momentum_threshold(self) -> float:
        return self.thresholds.momentum_threshold

    @property
    def confirmation_ticks(self) -> int:
        return self.thresholds.confirmation_ticks


# ---------------------------------------------------------------------------
# Dataclasses — Entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryConfig:
    min_edge: float
    min_confidence: float
    require_signal_confirmation: bool
    max_entry_price: float
    min_entry_price: float


# ---------------------------------------------------------------------------
# Dataclasses — Exit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeExitConfig:
    enabled: bool
    max_hold_seconds: float


@dataclass(frozen=True)
class ExitConfig:
    time_exit: TimeExitConfig


# ---------------------------------------------------------------------------
# Dataclasses — Risk (stop-loss, take-profit, position sizing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopLossDeltaConfig:
    max_loss_delta: float


@dataclass(frozen=True)
class StopLossPercentConfig:
    max_loss_pct: float


@dataclass(frozen=True)
class StopLossConfig:
    enabled: bool
    delta: StopLossDeltaConfig
    percent: StopLossPercentConfig


@dataclass(frozen=True)
class TakeProfitDeltaConfig:
    target_delta: float


@dataclass(frozen=True)
class TakeProfitPercentConfig:
    target_pct: float


@dataclass(frozen=True)
class TakeProfitConfig:
    enabled: bool
    delta: TakeProfitDeltaConfig
    percent: TakeProfitPercentConfig


@dataclass(frozen=True)
class PositionSizeConfig:
    position_size_usdc: float
    max_concurrent_positions: int


@dataclass(frozen=True)
class RiskConfig:
    position_size: PositionSizeConfig
    stop_loss: StopLossConfig
    take_profit: TakeProfitConfig
    allow_reentry: bool


# ---------------------------------------------------------------------------
# Dataclasses — Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionConfig:
    order_type: str
    slippage_tolerance: float
    retry_attempts: int
    retry_delay_seconds: float


# ---------------------------------------------------------------------------
# Dataclasses — Filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiltersConfig:
    market_type: str
    min_liquidity_usdc: float
    max_spread: float
    active_only: bool


# ---------------------------------------------------------------------------
# Dataclasses — Timing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimingConfig:
    poll_interval_seconds: float
    market_rotation_lead_seconds: float
    cooldown_after_exit_seconds: float


# ---------------------------------------------------------------------------
# Backward-compatible shims used by exit_rules.py / decisions.py / loop.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LegacyTakeProfitConfig:
    """Shim so ``exit_rules.take_profit.target_delta`` keeps working.

    Also carries the percent sub-config so exit_rules.py can evaluate
    percent-based thresholds when available.
    """
    enabled: bool
    target_delta: float
    percent: TakeProfitPercentConfig | None = None


@dataclass(frozen=True)
class _LegacyStopLossConfig:
    """Shim so ``exit_rules.stop_loss.max_loss_delta`` keeps working.

    Also carries the percent sub-config so exit_rules.py can evaluate
    percent-based thresholds when available.
    """
    enabled: bool
    max_loss_delta: float
    percent: StopLossPercentConfig | None = None


@dataclass(frozen=True)
class ExitRulesConfig:
    """Backward-compatible view expected by exit_rules.py / decisions.py."""
    take_profit: _LegacyTakeProfitConfig
    stop_loss: _LegacyStopLossConfig
    time_exit: TimeExitConfig
    allow_reentry: bool


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyConfig:
    strategy: StrategyMeta
    signals: SignalsConfig
    entry: EntryConfig
    exit: ExitConfig
    risk: RiskConfig
    execution: ExecutionConfig
    filters: FiltersConfig
    timing: TimingConfig

    @property
    def exit_rules(self) -> ExitRulesConfig:
        """Backward-compatible accessor used by loop.py, decisions.py, and
        exit_rules.py.  Assembles an :class:`ExitRulesConfig` from the new
        ``exit`` and ``risk`` sections so callers don't need to change yet."""
        return ExitRulesConfig(
            take_profit=_LegacyTakeProfitConfig(
                enabled=self.risk.take_profit.enabled,
                target_delta=self.risk.take_profit.delta.target_delta,
                percent=self.risk.take_profit.percent,
            ),
            stop_loss=_LegacyStopLossConfig(
                enabled=self.risk.stop_loss.enabled,
                max_loss_delta=self.risk.stop_loss.delta.max_loss_delta,
                percent=self.risk.stop_loss.percent,
            ),
            time_exit=self.exit.time_exit,
            allow_reentry=self.risk.allow_reentry,
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_DEFAULT_STRATEGY_PATH = Path("updown/strategies/btc_lag_arbitrage.yml")


def load_strategy_config(path: Path = _DEFAULT_STRATEGY_PATH) -> StrategyConfig:
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

    ctx_name = str(path)

    # -- strategy (required) -----------------------------------------------
    strat_raw = _require_mapping(_require(raw, "strategy", ctx_name), "strategy")
    strategy = StrategyMeta(
        name=_require_str(_require(strat_raw, "name", "strategy"), "strategy.name"),
        type=_require_str(_require(strat_raw, "type", "strategy"), "strategy.type"),
        version=_require_positive_int(
            _require(strat_raw, "version", "strategy"), "strategy.version",
        ),
        description=_require_str(
            _require(strat_raw, "description", "strategy"), "strategy.description",
        ),
    )

    # -- signals (required) ------------------------------------------------
    sig_raw = _require_mapping(_require(raw, "signals", ctx_name), "signals")
    thresholds = ThresholdsConfig(
        momentum_threshold=_require_positive_float(
            _require(sig_raw, "momentum_threshold", "signals"), "signals.momentum_threshold",
        ),
        confirmation_ticks=_require_positive_int(
            _require(sig_raw, "confirmation_ticks", "signals"), "signals.confirmation_ticks",
        ),
    )
    signals = SignalsConfig(
        type=_require_str(_require(sig_raw, "type", "signals"), "signals.type"),
        lookback_seconds=_require_positive_int(
            _require(sig_raw, "lookback_seconds", "signals"), "signals.lookback_seconds",
        ),
        smoothing=_require_str(_require(sig_raw, "smoothing", "signals"), "signals.smoothing"),
        thresholds=thresholds,
    )

    # -- entry (required) --------------------------------------------------
    ent_raw = _require_mapping(_require(raw, "entry", ctx_name), "entry")
    entry = EntryConfig(
        min_edge=_require_positive_float(
            _require(ent_raw, "min_edge", "entry"), "entry.min_edge",
        ),
        min_confidence=_require_positive_float(
            _require(ent_raw, "min_confidence", "entry"), "entry.min_confidence",
        ),
        require_signal_confirmation=_require_bool(
            _require(ent_raw, "require_signal_confirmation", "entry"),
            "entry.require_signal_confirmation",
        ),
        max_entry_price=_require_positive_float(
            _require(ent_raw, "max_entry_price", "entry"), "entry.max_entry_price",
        ),
        min_entry_price=_require_positive_float(
            _require(ent_raw, "min_entry_price", "entry"), "entry.min_entry_price",
        ),
    )

    # -- exit (required) ---------------------------------------------------
    exit_raw = _require_mapping(_require(raw, "exit", ctx_name), "exit")
    te_raw = _require_mapping(_require(exit_raw, "time_exit", "exit"), "exit.time_exit")
    time_exit = TimeExitConfig(
        enabled=_require_bool(
            _require(te_raw, "enabled", "exit.time_exit"), "exit.time_exit.enabled",
        ),
        max_hold_seconds=_require_positive_float(
            _require(te_raw, "max_hold_seconds", "exit.time_exit"),
            "exit.time_exit.max_hold_seconds",
        ),
    )
    exit_cfg = ExitConfig(time_exit=time_exit)

    # -- risk (required) ---------------------------------------------------
    risk_raw = _require_mapping(_require(raw, "risk", ctx_name), "risk")

    # position sizing
    position_size = PositionSizeConfig(
        position_size_usdc=_require_positive_float(
            _require(risk_raw, "position_size_usdc", "risk"),
            "risk.position_size_usdc",
        ),
        max_concurrent_positions=_require_positive_int(
            _require(risk_raw, "max_concurrent_positions", "risk"),
            "risk.max_concurrent_positions",
        ),
    )

    # stop_loss (with delta + percent sub-sections)
    sl_raw = _require_mapping(_require(risk_raw, "stop_loss", "risk"), "risk.stop_loss")
    sl_delta_raw = _require_mapping(_require(sl_raw, "delta", "risk.stop_loss"), "risk.stop_loss.delta")
    sl_pct_raw = _require_mapping(_require(sl_raw, "percent", "risk.stop_loss"), "risk.stop_loss.percent")
    stop_loss = StopLossConfig(
        enabled=_require_bool(
            _require(sl_raw, "enabled", "risk.stop_loss"), "risk.stop_loss.enabled",
        ),
        delta=StopLossDeltaConfig(
            max_loss_delta=_require_positive_float(
                _require(sl_delta_raw, "max_loss_delta", "risk.stop_loss.delta"),
                "risk.stop_loss.delta.max_loss_delta",
            ),
        ),
        percent=StopLossPercentConfig(
            max_loss_pct=_require_positive_float(
                _require(sl_pct_raw, "max_loss_pct", "risk.stop_loss.percent"),
                "risk.stop_loss.percent.max_loss_pct",
            ),
        ),
    )

    # take_profit (with delta + percent sub-sections)
    tp_raw = _require_mapping(_require(risk_raw, "take_profit", "risk"), "risk.take_profit")
    tp_delta_raw = _require_mapping(_require(tp_raw, "delta", "risk.take_profit"), "risk.take_profit.delta")
    tp_pct_raw = _require_mapping(_require(tp_raw, "percent", "risk.take_profit"), "risk.take_profit.percent")
    take_profit = TakeProfitConfig(
        enabled=_require_bool(
            _require(tp_raw, "enabled", "risk.take_profit"), "risk.take_profit.enabled",
        ),
        delta=TakeProfitDeltaConfig(
            target_delta=_require_positive_float(
                _require(tp_delta_raw, "target_delta", "risk.take_profit.delta"),
                "risk.take_profit.delta.target_delta",
            ),
        ),
        percent=TakeProfitPercentConfig(
            target_pct=_require_positive_float(
                _require(tp_pct_raw, "target_pct", "risk.take_profit.percent"),
                "risk.take_profit.percent.target_pct",
            ),
        ),
    )

    # allow_reentry
    allow_reentry = _require_bool(
        _require(risk_raw, "allow_reentry", "risk"), "risk.allow_reentry",
    )

    risk_cfg = RiskConfig(
        position_size=position_size,
        stop_loss=stop_loss,
        take_profit=take_profit,
        allow_reentry=allow_reentry,
    )

    # -- execution (required) ----------------------------------------------
    exec_raw = _require_mapping(_require(raw, "execution", ctx_name), "execution")
    execution = ExecutionConfig(
        order_type=_require_str(
            _require(exec_raw, "order_type", "execution"), "execution.order_type",
        ),
        slippage_tolerance=_require_positive_float(
            _require(exec_raw, "slippage_tolerance", "execution"),
            "execution.slippage_tolerance",
        ),
        retry_attempts=_require_positive_int(
            _require(exec_raw, "retry_attempts", "execution"),
            "execution.retry_attempts",
        ),
        retry_delay_seconds=_require_positive_float(
            _require(exec_raw, "retry_delay_seconds", "execution"),
            "execution.retry_delay_seconds",
        ),
    )

    # -- filters (required) ------------------------------------------------
    flt_raw = _require_mapping(_require(raw, "filters", ctx_name), "filters")
    filters = FiltersConfig(
        market_type=_require_str(
            _require(flt_raw, "market_type", "filters"), "filters.market_type",
        ),
        min_liquidity_usdc=_require_non_negative_float(
            _require(flt_raw, "min_liquidity_usdc", "filters"),
            "filters.min_liquidity_usdc",
        ),
        max_spread=_require_positive_float(
            _require(flt_raw, "max_spread", "filters"), "filters.max_spread",
        ),
        active_only=_require_bool(
            _require(flt_raw, "active_only", "filters"), "filters.active_only",
        ),
    )

    # -- timing (required) -------------------------------------------------
    tmg_raw = _require_mapping(_require(raw, "timing", ctx_name), "timing")
    timing = TimingConfig(
        poll_interval_seconds=_require_positive_float(
            _require(tmg_raw, "poll_interval_seconds", "timing"),
            "timing.poll_interval_seconds",
        ),
        market_rotation_lead_seconds=_require_non_negative_float(
            _require(tmg_raw, "market_rotation_lead_seconds", "timing"),
            "timing.market_rotation_lead_seconds",
        ),
        cooldown_after_exit_seconds=_require_non_negative_float(
            _require(tmg_raw, "cooldown_after_exit_seconds", "timing"),
            "timing.cooldown_after_exit_seconds",
        ),
    )

    return StrategyConfig(
        strategy=strategy,
        signals=signals,
        entry=entry,
        exit=exit_cfg,
        risk=risk_cfg,
        execution=execution,
        filters=filters,
        timing=timing,
    )
