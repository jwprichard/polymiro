"""Shared fixtures, factory functions, and mock factories for the updown test suite.

Production Module Classification
================================

**Pure modules (no mocking needed for core logic tests):**
- updown/types.py -- dataclasses, MarketState enum, transition logic
- updown/signal.py -- compute_signal() is pure math (but reads module-level
  SCALE_FACTOR and MIN_BTC_PCT_CHANGE from common.config at import time)
- updown/exit_rules.py -- check_exit() is pure: no I/O, no side effects
- updown/decisions.py -- evaluate_entry/exit/expiry are pure given a TickContext

**Modules requiring network mocks:**
- updown/executor.py -- py-clob-client (Polymarket CLOB), atomic file writes
- updown/loop.py -- aiohttp (Gamma REST), asyncio queues, Binance/Polymarket WS
- updown/polymarket_ws.py -- WebSocket + REST (requests.get for /book)
- updown/binance_ws.py -- WebSocket (Binance trade stream)
- updown/pnl/gamma_client.py -- requests.get to Gamma API
- updown/pnl/tracker.py -- calls gamma_client + atomic file writes

**Patching common.config values:**
    Use ``monkeypatch.setattr(config, "ATTR_NAME", value)`` for per-test
    overrides, or the ``tmp_data_dir`` fixture below for filesystem paths.
    Do NOT modify ``os.environ`` directly -- config.py reads env vars at
    import time, so setattr on the config module is the correct approach.

**Async test pattern (pytest-asyncio):**
    Mark async tests with ``@pytest.mark.asyncio`` and use function-scoped
    event loops (the default).  Example::

        import asyncio
        from unittest.mock import AsyncMock, patch

        @pytest.mark.asyncio
        async def test_example_async():
            # Mock asyncio.sleep to avoid real delays:
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await some_coroutine()
                mock_sleep.assert_awaited()

            # Mock asyncio.wait_for to control timeouts:
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wf:
                mock_wf.return_value = {"status": "ok"}
                result = await asyncio.wait_for(some_coro(), timeout=5)

            # Mock asyncio.Queue for controlled tick injection:
            q = asyncio.Queue()
            await q.put(some_tick)
            tick = await q.get()
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from common import config
from updown import executor
from updown import loop as _loop_module
from updown.exit_rules import ExitSignal
from updown.strategy_config import (
    EntryConfig,
    ExitConfig,
    ExecutionConfig,
    ExitRulesConfig,
    FiltersConfig,
    PositionSizeConfig,
    RiskConfig,
    SignalsConfig,
    StopLossConfig,
    StopLossDeltaConfig,
    StopLossPercentConfig,
    StrategyConfig,
    StrategyMeta,
    TakeProfitConfig,
    TakeProfitDeltaConfig,
    TakeProfitPercentConfig,
    ThresholdsConfig,
    TimeExitConfig,
    TimingConfig,
    _LegacyStopLossConfig,
    _LegacyTakeProfitConfig,
)
from updown.types import (
    MarketSnapshot,
    MarketState,
    OrderResult,
    SignalResult,
    TickContext,
    TradeIntent,
)
from updown.loop import TrackedMarket


# ═══════════════════════════════════════════════════════════════════════════
# pytest-asyncio configuration
# ═══════════════════════════════════════════════════════════════════════════

# Function-scoped event loops (one fresh loop per test function).
# This is the default for pytest-asyncio but we declare it explicitly
# so the intent is clear and the suite is resilient to default changes.
#
# NOTE: If pytest-asyncio >= 0.23 is installed, you may also set
#   [tool.pytest.ini_options]  asyncio_mode = "auto"
# in pyproject.toml to auto-detect async tests without @pytest.mark.asyncio.


# ═══════════════════════════════════════════════════════════════════════════
# Strategy YAML dict builder
# ═══════════════════════════════════════════════════════════════════════════


def make_strategy_yaml_dict(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid strategy YAML dict matching btc_lag_arbitrage.yml.

    Returns a plain dict that ``load_strategy_config`` can parse when
    written to a YAML file.  Callers can modify individual fields before
    feeding it to the loader::

        d = make_strategy_yaml_dict()
        d["entry"]["min_edge"] = 0.10
        # write to tmp YAML and load ...

    Keyword arguments are merged shallowly into top-level sections.
    For example: ``make_strategy_yaml_dict(entry={"min_edge": 0.10})``
    replaces the entire ``entry`` section.
    """
    base: dict[str, Any] = {
        "strategy": {
            "name": "test_strategy",
            "type": "momentum_lag",
            "version": 1,
            "description": "Test strategy for unit tests.",
        },
        "signals": {
            "type": "momentum",
            "lookback_seconds": 300,
            "smoothing": "ema",
            "momentum_threshold": 0.005,
            "confirmation_ticks": 2,
        },
        "entry": {
            "min_edge": 0.05,
            "min_confidence": 0.6,
            "require_signal_confirmation": True,
            "max_entry_price": 0.95,
            "min_entry_price": 0.05,
        },
        "exit": {
            "time_exit": {
                "enabled": True,
                "max_hold_seconds": 240.0,
            },
        },
        "risk": {
            "position_size_usdc": 5.0,
            "max_concurrent_positions": 1,
            "stop_loss": {
                "enabled": True,
                "delta": {"max_loss_delta": 0.04},
                "percent": {"max_loss_pct": 0.08},
            },
            "take_profit": {
                "enabled": True,
                "delta": {"target_delta": 0.06},
                "percent": {"target_pct": 0.12},
            },
            "allow_reentry": False,
        },
        "execution": {
            "order_type": "limit",
            "slippage_tolerance": 0.01,
            "retry_attempts": 2,
            "retry_delay_seconds": 1.0,
        },
        "filters": {
            "market_type": "btc_5min_updown",
            "min_liquidity_usdc": 50.0,
            "max_spread": 0.08,
            "active_only": True,
        },
        "timing": {
            "poll_interval_seconds": 5.0,
            "market_rotation_lead_seconds": 30.0,
            "cooldown_after_exit_seconds": 10.0,
        },
    }
    for key, value in overrides.items():
        base[key] = value
    return base


# ═══════════════════════════════════════════════════════════════════════════
# StrategyConfig factory
# ═══════════════════════════════════════════════════════════════════════════


def make_strategy_config(
    *,
    edge_threshold: float = 0.05,
    stop_loss_delta: float = 0.04,
    take_profit_delta: float = 0.06,
    max_hold_seconds: float = 240.0,
    allow_reentry: bool = False,
    stop_loss_enabled: bool = True,
    take_profit_enabled: bool = True,
    time_exit_enabled: bool = True,
    position_size_usdc: float = 5.0,
    max_concurrent_positions: int = 1,
    slippage_tolerance: float = 0.01,
    stop_loss_pct: float = 0.08,
    take_profit_pct: float = 0.12,
    min_confidence: float = 0.6,
    require_signal_confirmation: bool = True,
    max_entry_price: float = 0.95,
    min_entry_price: float = 0.05,
) -> StrategyConfig:
    """Build a valid StrategyConfig from in-memory values (no YAML file).

    Keyword arguments expose the most commonly varied fields so parametrized
    tests can override them directly::

        cfg = make_strategy_config(edge_threshold=0.10, allow_reentry=True)
    """
    return StrategyConfig(
        strategy=StrategyMeta(
            name="test_strategy",
            type="momentum_lag",
            version=1,
            description="Test strategy.",
        ),
        signals=SignalsConfig(
            type="momentum",
            lookback_seconds=300,
            smoothing="ema",
            thresholds=ThresholdsConfig(
                momentum_threshold=0.005,
                confirmation_ticks=2,
            ),
        ),
        entry=EntryConfig(
            min_edge=edge_threshold,
            min_confidence=min_confidence,
            require_signal_confirmation=require_signal_confirmation,
            max_entry_price=max_entry_price,
            min_entry_price=min_entry_price,
        ),
        exit=ExitConfig(
            time_exit=TimeExitConfig(
                enabled=time_exit_enabled,
                max_hold_seconds=max_hold_seconds,
            ),
        ),
        risk=RiskConfig(
            position_size=PositionSizeConfig(
                position_size_usdc=position_size_usdc,
                max_concurrent_positions=max_concurrent_positions,
            ),
            stop_loss=StopLossConfig(
                enabled=stop_loss_enabled,
                delta=StopLossDeltaConfig(max_loss_delta=stop_loss_delta),
                percent=StopLossPercentConfig(max_loss_pct=stop_loss_pct),
            ),
            take_profit=TakeProfitConfig(
                enabled=take_profit_enabled,
                delta=TakeProfitDeltaConfig(target_delta=take_profit_delta),
                percent=TakeProfitPercentConfig(target_pct=take_profit_pct),
            ),
            allow_reentry=allow_reentry,
        ),
        execution=ExecutionConfig(
            order_type="limit",
            slippage_tolerance=slippage_tolerance,
            retry_attempts=2,
            retry_delay_seconds=1.0,
        ),
        filters=FiltersConfig(
            market_type="btc_5min_updown",
            min_liquidity_usdc=50.0,
            max_spread=0.08,
            active_only=True,
        ),
        timing=TimingConfig(
            poll_interval_seconds=5.0,
            market_rotation_lead_seconds=30.0,
            cooldown_after_exit_seconds=10.0,
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ExitRulesConfig factory
# ═══════════════════════════════════════════════════════════════════════════


def make_exit_rules_config(
    *,
    stop_loss_enabled: bool = True,
    stop_loss_delta: float = 0.04,
    stop_loss_pct: Optional[float] = 0.08,
    take_profit_enabled: bool = True,
    take_profit_delta: float = 0.06,
    take_profit_pct: Optional[float] = 0.12,
    time_exit_enabled: bool = True,
    max_hold_seconds: float = 240.0,
    allow_reentry: bool = False,
) -> ExitRulesConfig:
    """Build an ExitRulesConfig (the backward-compatible shim used by exit_rules.py)."""
    return ExitRulesConfig(
        stop_loss=_LegacyStopLossConfig(
            enabled=stop_loss_enabled,
            max_loss_delta=stop_loss_delta,
            percent=StopLossPercentConfig(max_loss_pct=stop_loss_pct) if stop_loss_pct is not None else None,
        ),
        take_profit=_LegacyTakeProfitConfig(
            enabled=take_profit_enabled,
            target_delta=take_profit_delta,
            percent=TakeProfitPercentConfig(target_pct=take_profit_pct) if take_profit_pct is not None else None,
        ),
        time_exit=TimeExitConfig(
            enabled=time_exit_enabled,
            max_hold_seconds=max_hold_seconds,
        ),
        allow_reentry=allow_reentry,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TickContext factory
# ═══════════════════════════════════════════════════════════════════════════


def make_tick_context(
    *,
    tick_price: float = 67_000.0,
    tick_timestamp_ms: int = 1_700_000_000_000,
    open_price: float = 67_000.0,
    yes_price: float = 0.50,
    no_price: float = 0.50,
    price_age_ms: int = 100,
    market_id: str = "0xtest_market_id",
    question: str = "Will BTC go up in the next 5 minutes?",
    token_id: str = "token_abc123",
    expiry_time: float = 1_700_000_300.0,
    state: MarketState = MarketState.IDLE,
    entry_price: Optional[float] = None,
    entry_time: Optional[float] = None,
    entry_side: Optional[str] = None,
    entry_size_usdc: Optional[float] = None,
    strategy_config: Optional[StrategyConfig] = None,
) -> TickContext:
    """Build a TickContext with sensible defaults and keyword overrides.

    Default scenario: BTC at ~67000, market at 50/50, IDLE state, no
    open position, ~5 minutes until expiry.
    """
    return TickContext(
        tick_price=tick_price,
        tick_timestamp_ms=tick_timestamp_ms,
        open_price=open_price,
        yes_price=yes_price,
        no_price=no_price,
        price_age_ms=price_age_ms,
        market_id=market_id,
        question=question,
        token_id=token_id,
        expiry_time=expiry_time,
        state=state,
        entry_price=entry_price,
        entry_time=entry_time,
        entry_side=entry_side,
        entry_size_usdc=entry_size_usdc,
        strategy_config=strategy_config or make_strategy_config(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# TrackedMarket factory
# ═══════════════════════════════════════════════════════════════════════════


def make_tracked_market(
    *,
    condition_id: str = "0xtest_condition_id",
    question: str = "Will BTC go up in the next 5 minutes?",
    asset_ids: Optional[list[str]] = None,
    expiry_time: float = 1_700_000_300.0,
    state: MarketState = MarketState.IDLE,
    cooldown_until: float = 0.0,
    discovered_at: float = 0.0,
    entry_price: Optional[float] = None,
    entry_time: Optional[float] = None,
    entry_side: Optional[str] = None,
    entry_size_usdc: Optional[float] = None,
) -> TrackedMarket:
    """Build a TrackedMarket with sensible defaults."""
    return TrackedMarket(
        condition_id=condition_id,
        question=question,
        asset_ids=asset_ids if asset_ids is not None else ["token_abc123"],
        expiry_time=expiry_time,
        state=state,
        cooldown_until=cooldown_until,
        discovered_at=discovered_at or time.time(),
        entry_price=entry_price,
        entry_time=entry_time,
        entry_side=entry_side,
        entry_size_usdc=entry_size_usdc,
    )


# ═══════════════════════════════════════════════════════════════════════════
# SignalResult factory
# ═══════════════════════════════════════════════════════════════════════════


def make_signal_result(
    *,
    direction: str = "YES",
    implied_probability: float = 0.55,
    market_price: float = 0.50,
    edge: float = 0.05,
    should_trade: bool = True,
) -> SignalResult:
    """Build a SignalResult with sensible defaults."""
    return SignalResult(
        direction=direction,
        implied_probability=implied_probability,
        market_price=market_price,
        edge=edge,
        should_trade=should_trade,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TradeIntent factory
# ═══════════════════════════════════════════════════════════════════════════


def make_trade_intent(
    *,
    market_id: str = "0xtest_market_id",
    token_id: str = "token_abc123",
    side: str = "buy",
    outcome: str = "yes",
    size_usdc: float = 5.0,
    signal: Optional[SignalResult] = None,
    market: Optional[MarketSnapshot] = None,
    reason: str = "test entry",
    signal_price: Optional[float] = None,
    tick_timestamp_ms: int = 0,
) -> TradeIntent:
    """Build a TradeIntent with sensible defaults."""
    return TradeIntent(
        market_id=market_id,
        token_id=token_id,
        side=side,
        outcome=outcome,
        size_usdc=size_usdc,
        signal=signal or make_signal_result(),
        market=market or MarketSnapshot(
            market_id=market_id,
            question="Will BTC go up in the next 5 minutes?",
            token_id=token_id,
            yes_price=0.50,
            no_price=0.50,
            spread=0.02,
            timestamp_ms=int(time.time() * 1000),
        ),
        reason=reason,
        signal_price=signal_price,
        tick_timestamp_ms=tick_timestamp_ms,
    )


# ═══════════════════════════════════════════════════════════════════════════
# OrderResult factory
# ═══════════════════════════════════════════════════════════════════════════


def make_order_result(
    *,
    intent: Optional[TradeIntent] = None,
    success: bool = True,
    order_id: Optional[str] = "dry-test-order-001",
    filled_price: Optional[float] = 0.50,
    filled_size: Optional[float] = 5.0,
    error: Optional[str] = None,
    timestamp_ms: int = 0,
) -> OrderResult:
    """Build an OrderResult with sensible defaults."""
    return OrderResult(
        intent=intent or make_trade_intent(),
        success=success,
        order_id=order_id,
        filled_price=filled_price,
        filled_size=filled_size,
        error=error,
        timestamp_ms=timestamp_ms or int(time.time() * 1000),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Filesystem isolation fixture
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all updown data paths to a temporary directory.

    Patches:
    - config.UPDOWN_DATA_DIR  -> tmp_path / "updown_data"
    - config.UPDOWN_TRADES_FILE -> tmp_path / "updown_data" / "updown_trades.json"
    - config.PNL_REPORT_FILE -> tmp_path / "updown_data" / "pnl_report.json"

    Returns the temporary data directory path so tests can inspect written files.
    """
    data_dir = tmp_path / "updown_data"
    data_dir.mkdir()

    monkeypatch.setattr(config, "UPDOWN_DATA_DIR", data_dir)
    monkeypatch.setattr(config, "UPDOWN_TRADES_FILE", data_dir / "updown_trades.json")
    monkeypatch.setattr(config, "PNL_REPORT_FILE", data_dir / "pnl_report.json")

    return data_dir


# ═══════════════════════════════════════════════════════════════════════════
# Executor globals isolation (autouse)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def isolate_executor_globals():
    """Reset executor module-level mutable state between every test.

    Prevents cross-test contamination of:
    - executor.slippage_rejections (int counter)
    - executor._latency_samples (list of latency measurements)
    - executor._clob_client (cached ClobClient instance)
    """
    # Save originals
    orig_rejections = executor.slippage_rejections
    orig_samples = list(executor._latency_samples)
    orig_client = executor._clob_client

    # Reset to clean state
    executor.slippage_rejections = 0
    executor._latency_samples.clear()
    executor._clob_client = None

    yield

    # Restore originals (defensive -- mainly to not accumulate state if
    # a test runner reuses the process for non-updown tests afterwards)
    executor.slippage_rejections = orig_rejections
    executor._latency_samples.clear()
    executor._latency_samples.extend(orig_samples)
    executor._clob_client = orig_client


# ═══════════════════════════════════════════════════════════════════════════
# Loop module globals isolation (autouse)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def isolate_loop_globals():
    """Reset loop module-level mutable state between every test.

    Prevents cross-test contamination of:
    - loop.ticks_drained (int counter)
    """
    orig_ticks_drained = _loop_module.ticks_drained
    _loop_module.ticks_drained = 0

    yield

    _loop_module.ticks_drained = orig_ticks_drained


# ═══════════════════════════════════════════════════════════════════════════
# Mock: aiohttp.ClientSession
# ═══════════════════════════════════════════════════════════════════════════


def mock_aiohttp_session(
    json_responses: Optional[dict[str, Any]] = None,
) -> MagicMock:
    """Build a MagicMock mimicking ``aiohttp.ClientSession``.

    Parameters
    ----------
    json_responses:
        Mapping of URL substrings to JSON response bodies.  When the mock
        ``session.get(url)`` is called, the first matching key is used.
        If no key matches, returns ``{}``.

    Usage::

        session = mock_aiohttp_session({
            "/markets": [{"conditionId": "0x123", "closed": False}],
        })
        async with session.get("https://gamma-api.polymarket.com/markets") as resp:
            data = await resp.json()  # -> [{"conditionId": "0x123", ...}]
    """
    responses = json_responses or {}

    session = MagicMock()

    def _make_response(url: str, **kwargs: Any) -> MagicMock:
        body: Any = {}
        for pattern, resp_body in responses.items():
            if pattern in str(url):
                body = resp_body
                break

        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=body)
        resp.text = AsyncMock(return_value=json.dumps(body))
        resp.raise_for_status = MagicMock()

        # Support ``async with session.get(url) as resp:``
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.get = MagicMock(side_effect=_make_response)
    session.post = MagicMock(side_effect=_make_response)

    # Support ``async with aiohttp.ClientSession() as session:``
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    return session


# ═══════════════════════════════════════════════════════════════════════════
# Mock: WebSocket (Binance / Polymarket)
# ═══════════════════════════════════════════════════════════════════════════


def mock_websocket(messages: list[str]) -> MagicMock:
    """Build a mock WebSocket connection that yields *messages* as an async iterable.

    Suitable for both Binance and Polymarket WS test scenarios.  The mock
    supports ``async for msg in ws:`` iteration and ``await ws.recv()``.

    Parameters
    ----------
    messages:
        Raw JSON message strings to yield.  After all messages are consumed,
        iteration stops (simulating a clean disconnect).

    Usage::

        ws = mock_websocket([
            '{"e":"trade","s":"BTCUSDT","p":"67123.45","T":1700000000000}',
            '{"e":"trade","s":"BTCUSDT","p":"67130.00","T":1700000001000}',
        ])
        async for msg in ws:
            data = json.loads(msg)
    """
    ws = MagicMock()

    msg_iter = iter(messages)

    async def _recv() -> str:
        try:
            return next(msg_iter)
        except StopIteration:
            raise StopAsyncIteration()

    ws.recv = AsyncMock(side_effect=_recv)

    # Support ``async for msg in ws:``
    async def _aiter():
        for msg in messages:
            yield msg

    ws.__aiter__ = lambda self: _aiter()

    # Support ``await ws.send(data)``
    ws.send = AsyncMock()

    # Support ``await ws.close()``
    ws.close = AsyncMock()

    return ws


# ═══════════════════════════════════════════════════════════════════════════
# Mock: py-clob-client ClobClient
# ═══════════════════════════════════════════════════════════════════════════


def mock_clob_client(
    *,
    order_id: str = "live-test-order-001",
    status: str = "matched",
) -> MagicMock:
    """Build a MagicMock mimicking ``py_clob_client.client.ClobClient``.

    The ``create_and_post_order`` method returns a dict matching the
    real Polymarket CLOB response shape.

    Parameters
    ----------
    order_id:
        The ``orderID`` field in the response.
    status:
        The ``status`` field (e.g. "matched", "live", "delayed").
    """
    client = MagicMock()
    client.create_and_post_order.return_value = {
        "orderID": order_id,
        "status": status,
    }
    return client


# ═══════════════════════════════════════════════════════════════════════════
# Mock: Gamma API market response
# ═══════════════════════════════════════════════════════════════════════════


def mock_gamma_response(
    *,
    condition_id: str = "0xtest_condition_id",
    closed: bool = False,
    accepting_orders: bool = True,
    outcome_prices: Optional[list[str]] = None,
    outcomes: Optional[list[str]] = None,
    question: str = "Will BTC go up?",
) -> dict[str, Any]:
    """Build a Gamma API market record with configurable resolution state.

    Parameters
    ----------
    condition_id:
        Hex condition ID for the market.
    closed:
        Whether the market has closed.
    accepting_orders:
        Whether the market is still accepting orders.
    outcome_prices:
        List of price strings, e.g. ``["1", "0"]`` for a resolved YES market.
        Defaults to ``["0.50", "0.50"]`` (unresolved).
    outcomes:
        Outcome labels.  Defaults to ``["Yes", "No"]``.

    Usage::

        # Unresolved market
        record = mock_gamma_response()

        # Resolved: YES won
        record = mock_gamma_response(closed=True, accepting_orders=False,
                                      outcome_prices=["1", "0"])
    """
    return {
        "conditionId": condition_id,
        "questionID": f"q_{condition_id[2:10]}",
        "question": question,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "outcomePrices": outcome_prices if outcome_prices is not None else ["0.50", "0.50"],
        "outcomes": outcomes if outcomes is not None else ["Yes", "No"],
        "tokens": [
            {"token_id": "token_yes_123", "outcome": "Yes"},
            {"token_id": "token_no_456", "outcome": "No"},
        ],
    }
