"""Tests for updown/strategy_config.py — validation helpers and YAML loader.

Covers:
- Each validation helper with valid and invalid inputs
- load_strategy_config() integration smoke test against the real YAML
- load_strategy_config() with tmp_path YAML: missing fields, invalid types, bad file
- StrategyConfig.exit_rules backward-compatible property
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from updown.strategy_config import (
    ExitRulesConfig,
    StrategyConfig,
    _LegacyStopLossConfig,
    _LegacyTakeProfitConfig,
    _require,
    _require_bool,
    _require_mapping,
    _require_non_negative_float,
    _require_positive_float,
    _require_positive_int,
    _require_str,
    load_strategy_config,
)

# Import factories from conftest (pytest auto-discovers them, but explicit
# import keeps the IDE happy and makes dependencies clear).
from updown.tests.conftest import make_strategy_config, make_strategy_yaml_dict


# ═══════════════════════════════════════════════════════════════════════════
# Helper: write a YAML dict to a tmp file and return its Path
# ═══════════════════════════════════════════════════════════════════════════


def _write_yaml(tmp_path: Path, data: dict, name: str = "strat.yml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require
# ═══════════════════════════════════════════════════════════════════════════


class TestRequire:
    def test_returns_value_when_present(self):
        assert _require({"a": 42}, "a", "ctx") == 42

    def test_returns_falsy_values(self):
        assert _require({"a": 0}, "a", "ctx") == 0
        assert _require({"a": False}, "a", "ctx") is False
        assert _require({"a": ""}, "a", "ctx") == ""
        assert _require({"a": None}, "a", "ctx") is None

    def test_raises_on_missing_key(self):
        with pytest.raises(ValueError, match="Missing required field 'x' in ctx"):
            _require({}, "x", "ctx")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_positive_float
# ═══════════════════════════════════════════════════════════════════════════


class TestRequirePositiveFloat:
    @pytest.mark.parametrize("val,expected", [
        (1, 1.0),
        (0.5, 0.5),
        (3, 3.0),
        ("2.5", 2.5),
    ])
    def test_valid(self, val, expected):
        assert _require_positive_float(val, "f") == expected

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            _require_positive_float(0, "f")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            _require_positive_float(-1.0, "f")

    @pytest.mark.parametrize("val", [None, "abc", [], {}])
    def test_non_numeric_raises(self, val):
        with pytest.raises(ValueError, match="must be a positive number"):
            _require_positive_float(val, "f")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_non_negative_float
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireNonNegativeFloat:
    @pytest.mark.parametrize("val,expected", [
        (0, 0.0),
        (0.0, 0.0),
        (1.5, 1.5),
        ("3", 3.0),
    ])
    def test_valid(self, val, expected):
        assert _require_non_negative_float(val, "f") == expected

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must be non-negative"):
            _require_non_negative_float(-0.01, "f")

    @pytest.mark.parametrize("val", [None, "abc", []])
    def test_non_numeric_raises(self, val):
        with pytest.raises(ValueError, match="must be a non-negative number"):
            _require_non_negative_float(val, "f")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_positive_int
# ═══════════════════════════════════════════════════════════════════════════


class TestRequirePositiveInt:
    @pytest.mark.parametrize("val,expected", [
        (1, 1),
        (99, 99),
        ("5", 5),
    ])
    def test_valid(self, val, expected):
        assert _require_positive_int(val, "n") == expected

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            _require_positive_int(0, "n")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            _require_positive_int(-3, "n")

    @pytest.mark.parametrize("val", [None, "abc", []])
    def test_non_integer_raises(self, val):
        with pytest.raises(ValueError, match="must be a positive integer"):
            _require_positive_int(val, "n")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_bool
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireBool:
    def test_true(self):
        assert _require_bool(True, "b") is True

    def test_false(self):
        assert _require_bool(False, "b") is False

    @pytest.mark.parametrize("val", [1, 0, "true", None, "yes"])
    def test_non_bool_raises(self, val):
        with pytest.raises(ValueError, match="must be a boolean"):
            _require_bool(val, "b")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_str
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireStr:
    def test_valid(self):
        assert _require_str("hello", "s") == "hello"

    def test_empty_string_valid(self):
        assert _require_str("", "s") == ""

    @pytest.mark.parametrize("val", [123, True, None, []])
    def test_non_string_raises(self, val):
        with pytest.raises(ValueError, match="must be a string"):
            _require_str(val, "s")


# ═══════════════════════════════════════════════════════════════════════════
# Validation helpers — _require_mapping
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireMapping:
    def test_valid_dict(self):
        d = {"a": 1}
        assert _require_mapping(d, "ctx") is d

    def test_empty_dict_valid(self):
        d = {}
        assert _require_mapping(d, "ctx") is d

    @pytest.mark.parametrize("val", ["string", 42, [1, 2], True, None])
    def test_non_dict_raises(self, val):
        with pytest.raises(ValueError, match="must be a mapping"):
            _require_mapping(val, "ctx")


# ═══════════════════════════════════════════════════════════════════════════
# Integration smoke test — real btc_lag_arbitrage.yml
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadRealYAML:
    """Load the actual strategy YAML and verify key fields are parsed."""

    REAL_PATH = Path("updown/strategies/btc_lag_arbitrage.yml")

    @pytest.fixture()
    def cfg(self) -> StrategyConfig:
        return load_strategy_config(self.REAL_PATH)

    def test_returns_strategy_config(self, cfg):
        assert isinstance(cfg, StrategyConfig)

    def test_strategy_meta(self, cfg):
        assert cfg.strategy.name == "btc_lag_arbitrage"
        assert cfg.strategy.type == "momentum_lag"
        assert cfg.strategy.version == 1

    def test_signals(self, cfg):
        assert cfg.signals.type == "momentum"
        assert cfg.signals.lookback_seconds == 300
        assert cfg.signals.smoothing == "ema"
        assert cfg.signals.momentum_threshold == 0.005
        assert cfg.signals.confirmation_ticks == 2

    def test_entry(self, cfg):
        assert cfg.entry.min_edge == 0.05
        assert cfg.entry.require_signal_confirmation is True

    def test_exit(self, cfg):
        assert cfg.exit.time_exit.enabled is True
        assert cfg.exit.time_exit.max_hold_seconds == 240.0

    def test_risk(self, cfg):
        assert cfg.risk.position_size.position_size_usdc == 5.0
        assert cfg.risk.stop_loss.enabled is True
        assert cfg.risk.stop_loss.delta.max_loss_delta == 0.04
        assert cfg.risk.take_profit.delta.target_delta == 0.06
        assert cfg.risk.allow_reentry is False

    def test_execution(self, cfg):
        assert cfg.execution.order_type == "limit"
        assert cfg.execution.retry_attempts == 2

    def test_filters(self, cfg):
        assert cfg.filters.market_type == "btc_5min_updown"
        assert cfg.filters.active_only is True

    def test_timing(self, cfg):
        assert cfg.timing.poll_interval_seconds == 5.0
        assert cfg.timing.cooldown_after_exit_seconds == 10.0


# ═══════════════════════════════════════════════════════════════════════════
# Loader — tmp_path YAML, valid round-trip
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadFromTmpPath:
    def test_round_trip_valid(self, tmp_path):
        """A valid YAML dict produced by the factory loads without error."""
        p = _write_yaml(tmp_path, make_strategy_yaml_dict())
        cfg = load_strategy_config(p)
        assert isinstance(cfg, StrategyConfig)
        assert cfg.strategy.name == "test_strategy"

    def test_overridden_field(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["entry"]["min_edge"] = 0.10
        cfg = load_strategy_config(_write_yaml(tmp_path, d))
        assert cfg.entry.min_edge == 0.10


# ═══════════════════════════════════════════════════════════════════════════
# Loader — missing required fields raise ValueError
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadMissingFields:
    @pytest.mark.parametrize("section", [
        "strategy", "signals", "entry", "exit", "risk",
        "execution", "filters", "timing",
    ])
    def test_missing_top_level_section(self, tmp_path, section):
        d = make_strategy_yaml_dict()
        del d[section]
        with pytest.raises(ValueError, match=f"Missing required field '{section}'"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_missing_nested_field(self, tmp_path):
        d = make_strategy_yaml_dict()
        del d["entry"]["min_edge"]
        with pytest.raises(ValueError, match="Missing required field 'min_edge'"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_missing_stop_loss_delta(self, tmp_path):
        d = make_strategy_yaml_dict()
        del d["risk"]["stop_loss"]["delta"]
        with pytest.raises(ValueError, match="Missing required field 'delta'"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_missing_time_exit(self, tmp_path):
        d = make_strategy_yaml_dict()
        del d["exit"]["time_exit"]
        with pytest.raises(ValueError, match="Missing required field 'time_exit'"):
            load_strategy_config(_write_yaml(tmp_path, d))


# ═══════════════════════════════════════════════════════════════════════════
# Loader — invalid types raise ValueError
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadInvalidTypes:
    def test_strategy_not_a_mapping(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["strategy"] = "not a dict"
        with pytest.raises(ValueError, match="must be a mapping"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_negative_min_edge(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["entry"]["min_edge"] = -0.01
        with pytest.raises(ValueError, match="must be positive"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_non_numeric_lookback(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["signals"]["lookback_seconds"] = "fast"
        with pytest.raises(ValueError, match="must be a positive integer"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_non_bool_enabled(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["risk"]["stop_loss"]["enabled"] = "yes"
        with pytest.raises(ValueError, match="must be a boolean"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_non_string_order_type(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["execution"]["order_type"] = 123
        with pytest.raises(ValueError, match="must be a string"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_zero_position_size(self, tmp_path):
        d = make_strategy_yaml_dict()
        d["risk"]["position_size_usdc"] = 0
        with pytest.raises(ValueError, match="must be positive"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_negative_min_liquidity(self, tmp_path):
        """min_liquidity_usdc uses _require_non_negative_float — negative should fail."""
        d = make_strategy_yaml_dict()
        d["filters"]["min_liquidity_usdc"] = -10
        with pytest.raises(ValueError, match="must be non-negative"):
            load_strategy_config(_write_yaml(tmp_path, d))

    def test_top_level_not_mapping(self, tmp_path):
        """A YAML file whose top-level value is a list (not a mapping)."""
        p = tmp_path / "bad.yml"
        p.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="Expected top-level mapping"):
            load_strategy_config(p)


# ═══════════════════════════════════════════════════════════════════════════
# Loader — non-existent file raises ValueError
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadNonExistentFile:
    def test_missing_file(self, tmp_path):
        missing = tmp_path / "does_not_exist.yml"
        with pytest.raises(ValueError, match="Strategy file not found"):
            load_strategy_config(missing)

    def test_invalid_yaml(self, tmp_path):
        bad = tmp_path / "bad.yml"
        bad.write_text("{ not valid yaml: [")
        with pytest.raises(ValueError, match="Invalid YAML"):
            load_strategy_config(bad)


# ═══════════════════════════════════════════════════════════════════════════
# StrategyConfig.exit_rules property
# ═══════════════════════════════════════════════════════════════════════════


class TestExitRulesProperty:
    """The exit_rules property assembles a backward-compatible ExitRulesConfig."""

    def test_returns_exit_rules_config(self):
        cfg = make_strategy_config()
        er = cfg.exit_rules
        assert isinstance(er, ExitRulesConfig)

    def test_take_profit_fields(self):
        cfg = make_strategy_config(take_profit_delta=0.08, take_profit_enabled=True)
        er = cfg.exit_rules
        assert isinstance(er.take_profit, _LegacyTakeProfitConfig)
        assert er.take_profit.enabled is True
        assert er.take_profit.target_delta == 0.08

    def test_take_profit_percent_passthrough(self):
        cfg = make_strategy_config(take_profit_pct=0.15)
        er = cfg.exit_rules
        assert er.take_profit.percent is not None
        assert er.take_profit.percent.target_pct == 0.15

    def test_stop_loss_fields(self):
        cfg = make_strategy_config(stop_loss_delta=0.03, stop_loss_enabled=False)
        er = cfg.exit_rules
        assert isinstance(er.stop_loss, _LegacyStopLossConfig)
        assert er.stop_loss.enabled is False
        assert er.stop_loss.max_loss_delta == 0.03

    def test_stop_loss_percent_passthrough(self):
        cfg = make_strategy_config(stop_loss_pct=0.10)
        er = cfg.exit_rules
        assert er.stop_loss.percent is not None
        assert er.stop_loss.percent.max_loss_pct == 0.10

    def test_time_exit_from_exit_section(self):
        cfg = make_strategy_config(time_exit_enabled=False, max_hold_seconds=120.0)
        er = cfg.exit_rules
        assert er.time_exit.enabled is False
        assert er.time_exit.max_hold_seconds == 120.0

    def test_allow_reentry_from_risk(self):
        cfg = make_strategy_config(allow_reentry=True)
        assert cfg.exit_rules.allow_reentry is True

    def test_exit_rules_frozen(self):
        cfg = make_strategy_config()
        er = cfg.exit_rules
        with pytest.raises(AttributeError):
            er.allow_reentry = True  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# SignalsConfig convenience accessors
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalsConvenienceAccessors:
    """The momentum_threshold / confirmation_ticks properties delegate to thresholds."""

    def test_momentum_threshold(self):
        cfg = make_strategy_config()
        assert cfg.signals.momentum_threshold == cfg.signals.thresholds.momentum_threshold

    def test_confirmation_ticks(self):
        cfg = make_strategy_config()
        assert cfg.signals.confirmation_ticks == cfg.signals.thresholds.confirmation_ticks
