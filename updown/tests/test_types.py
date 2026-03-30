"""Comprehensive unit tests for updown/types.py.

Covers:
- MarketState enum values
- All legal state transitions via transition() and validate_transition()
- Exhaustive illegal transition matrix raising InvalidTransitionError
- get_exchange_now_ms() with a PriceUpdate and with None
- Dataclass constructor smoke tests for all types (field assignment, frozen/unfrozen)
"""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError

import pytest

from updown.types import (
    InvalidTransitionError,
    MarketSnapshot,
    MarketState,
    NewMarket,
    OrderResult,
    PriceUpdate,
    SignalResult,
    TickContext,
    TickDecision,
    TradeIntent,
    get_exchange_now_ms,
    transition,
    validate_transition,
)

# Import conftest factories
from updown.tests.conftest import (
    make_signal_result,
    make_strategy_config,
    make_tick_context,
    make_trade_intent,
)


# ═══════════════════════════════════════════════════════════════════════════
# MarketState enum
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketStateEnum:
    """Verify enum members and their string values."""

    def test_all_members_present(self):
        members = {m.name for m in MarketState}
        assert members == {"IDLE", "ENTERING", "ENTERED", "EXITING", "COOLDOWN"}

    @pytest.mark.parametrize(
        "member, value",
        [
            (MarketState.IDLE, "idle"),
            (MarketState.ENTERING, "entering"),
            (MarketState.ENTERED, "entered"),
            (MarketState.EXITING, "exiting"),
            (MarketState.COOLDOWN, "cooldown"),
        ],
    )
    def test_member_values(self, member: MarketState, value: str):
        assert member.value == value


# ═══════════════════════════════════════════════════════════════════════════
# Legal transitions
# ═══════════════════════════════════════════════════════════════════════════

# Every legal (from, to) pair in the state machine.
LEGAL_TRANSITIONS: list[tuple[MarketState, MarketState]] = [
    (MarketState.IDLE, MarketState.ENTERING),
    (MarketState.ENTERING, MarketState.ENTERED),
    (MarketState.ENTERING, MarketState.IDLE),
    (MarketState.ENTERED, MarketState.EXITING),
    (MarketState.ENTERED, MarketState.IDLE),
    (MarketState.EXITING, MarketState.COOLDOWN),
    (MarketState.EXITING, MarketState.IDLE),
    (MarketState.COOLDOWN, MarketState.IDLE),
]


class TestLegalTransitions:
    """Every legal transition should succeed via both validate_transition and transition."""

    @pytest.mark.parametrize("current, target", LEGAL_TRANSITIONS)
    def test_validate_transition_does_not_raise(
        self, current: MarketState, target: MarketState
    ):
        # Should not raise
        validate_transition(current, target)

    @pytest.mark.parametrize("current, target", LEGAL_TRANSITIONS)
    def test_transition_returns_target(
        self, current: MarketState, target: MarketState
    ):
        result = transition(current, target)
        assert result is target


# ═══════════════════════════════════════════════════════════════════════════
# Illegal transitions (exhaustive matrix)
# ═══════════════════════════════════════════════════════════════════════════

# Build every (from, to) pair that is NOT in the legal set.
_all_states = list(MarketState)
_legal_set = set(LEGAL_TRANSITIONS)
ILLEGAL_TRANSITIONS: list[tuple[MarketState, MarketState]] = [
    (s_from, s_to)
    for s_from in _all_states
    for s_to in _all_states
    if (s_from, s_to) not in _legal_set
]


class TestIllegalTransitions:
    """Every illegal transition must raise InvalidTransitionError."""

    @pytest.mark.parametrize("current, target", ILLEGAL_TRANSITIONS)
    def test_validate_transition_raises(
        self, current: MarketState, target: MarketState
    ):
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition(current, target)
        # Error message should mention both state names
        msg = str(exc_info.value)
        assert current.name in msg
        assert target.name in msg

    @pytest.mark.parametrize("current, target", ILLEGAL_TRANSITIONS)
    def test_transition_raises(
        self, current: MarketState, target: MarketState
    ):
        with pytest.raises(InvalidTransitionError):
            transition(current, target)


class TestTransitionEdgeCases:
    """Additional transition logic checks."""

    def test_self_transitions_are_illegal(self):
        """No state should be able to transition to itself."""
        for state in MarketState:
            with pytest.raises(InvalidTransitionError):
                validate_transition(state, state)

    def test_error_message_lists_allowed_transitions(self):
        """The error message should include the set of allowed targets."""
        with pytest.raises(InvalidTransitionError, match="Allowed transitions"):
            validate_transition(MarketState.IDLE, MarketState.ENTERED)

    def test_transition_returns_exact_enum_member(self):
        result = transition(MarketState.IDLE, MarketState.ENTERING)
        assert result is MarketState.ENTERING
        assert isinstance(result, MarketState)


# ═══════════════════════════════════════════════════════════════════════════
# get_exchange_now_ms
# ═══════════════════════════════════════════════════════════════════════════


class TestGetExchangeNowMs:
    """Test the canonical time source function."""

    def test_with_price_update_returns_tick_timestamp(self):
        tick = PriceUpdate(symbol="BTCUSDT", price=67000.0, timestamp_ms=1_700_000_000_000)
        result = get_exchange_now_ms(tick)
        assert result == 1_700_000_000_000

    def test_with_none_returns_wall_clock(self):
        before = int(time.time() * 1000)
        result = get_exchange_now_ms(None)
        after = int(time.time() * 1000)
        assert before <= result <= after

    def test_with_no_argument_returns_wall_clock(self):
        """Default argument is None, so calling with no args should use wall clock."""
        before = int(time.time() * 1000)
        result = get_exchange_now_ms()
        after = int(time.time() * 1000)
        assert before <= result <= after

    def test_returns_int(self):
        tick = PriceUpdate(symbol="BTCUSDT", price=67000.0, timestamp_ms=42)
        assert isinstance(get_exchange_now_ms(tick), int)
        assert isinstance(get_exchange_now_ms(None), int)


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: PriceUpdate
# ═══════════════════════════════════════════════════════════════════════════


class TestPriceUpdate:
    def test_field_assignment(self):
        pu = PriceUpdate(symbol="BTCUSDT", price=67123.45, timestamp_ms=1_700_000_000_000)
        assert pu.symbol == "BTCUSDT"
        assert pu.price == 67123.45
        assert pu.timestamp_ms == 1_700_000_000_000

    def test_is_mutable(self):
        pu = PriceUpdate(symbol="BTCUSDT", price=67000.0, timestamp_ms=0)
        pu.price = 68000.0
        assert pu.price == 68000.0

    def test_equality(self):
        a = PriceUpdate(symbol="BTCUSDT", price=67000.0, timestamp_ms=100)
        b = PriceUpdate(symbol="BTCUSDT", price=67000.0, timestamp_ms=100)
        assert a == b


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: MarketSnapshot
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketSnapshot:
    def test_field_assignment(self):
        ms = MarketSnapshot(
            market_id="0xabc",
            question="Will BTC go up?",
            token_id="tok_123",
            yes_price=0.55,
            no_price=0.45,
            spread=0.02,
            timestamp_ms=1_700_000_000_000,
        )
        assert ms.market_id == "0xabc"
        assert ms.question == "Will BTC go up?"
        assert ms.token_id == "tok_123"
        assert ms.yes_price == 0.55
        assert ms.no_price == 0.45
        assert ms.spread == 0.02
        assert ms.timestamp_ms == 1_700_000_000_000

    def test_is_mutable(self):
        ms = MarketSnapshot(
            market_id="0x1", question="q", token_id="t",
            yes_price=0.5, no_price=0.5, spread=0.0, timestamp_ms=0,
        )
        ms.yes_price = 0.6
        assert ms.yes_price == 0.6


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: SignalResult
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalResult:
    def test_field_assignment(self):
        sr = SignalResult(
            direction="YES",
            implied_probability=0.60,
            market_price=0.50,
            edge=0.10,
            should_trade=True,
        )
        assert sr.direction == "YES"
        assert sr.implied_probability == 0.60
        assert sr.market_price == 0.50
        assert sr.edge == 0.10
        assert sr.should_trade is True

    def test_factory_defaults(self):
        sr = make_signal_result()
        assert sr.direction == "YES"
        assert sr.should_trade is True

    def test_is_mutable(self):
        sr = make_signal_result()
        sr.edge = 0.20
        assert sr.edge == 0.20


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: TradeIntent
# ═══════════════════════════════════════════════════════════════════════════


class TestTradeIntent:
    def test_field_assignment(self):
        signal = make_signal_result()
        market = MarketSnapshot(
            market_id="0xabc", question="q", token_id="t",
            yes_price=0.5, no_price=0.5, spread=0.02, timestamp_ms=100,
        )
        ti = TradeIntent(
            market_id="0xabc",
            token_id="tok_1",
            side="buy",
            outcome="yes",
            size_usdc=5.0,
            signal=signal,
            market=market,
            reason="momentum entry",
            signal_price=0.50,
            tick_timestamp_ms=1_700_000_000_000,
        )
        assert ti.market_id == "0xabc"
        assert ti.side == "buy"
        assert ti.outcome == "yes"
        assert ti.size_usdc == 5.0
        assert ti.signal is signal
        assert ti.market is market
        assert ti.reason == "momentum entry"
        assert ti.signal_price == 0.50
        assert ti.tick_timestamp_ms == 1_700_000_000_000

    def test_optional_defaults(self):
        ti = make_trade_intent()
        assert ti.signal_price is None
        assert ti.tick_timestamp_ms == 0

    def test_is_mutable(self):
        ti = make_trade_intent()
        ti.size_usdc = 10.0
        assert ti.size_usdc == 10.0


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: OrderResult
# ═══════════════════════════════════════════════════════════════════════════


class TestOrderResult:
    def test_field_assignment(self):
        intent = make_trade_intent()
        order = OrderResult(
            intent=intent,
            success=True,
            order_id="order-001",
            filled_price=0.50,
            filled_size=5.0,
            error=None,
            timestamp_ms=1_700_000_000_000,
        )
        assert order.intent is intent
        assert order.success is True
        assert order.order_id == "order-001"
        assert order.filled_price == 0.50
        assert order.filled_size == 5.0
        assert order.error is None
        assert order.timestamp_ms == 1_700_000_000_000

    def test_optional_defaults(self):
        intent = make_trade_intent()
        order = OrderResult(intent=intent, success=False)
        assert order.order_id is None
        assert order.filled_price is None
        assert order.filled_size is None
        assert order.error is None
        assert order.timestamp_ms == 0

    def test_failed_order(self):
        intent = make_trade_intent()
        order = OrderResult(
            intent=intent,
            success=False,
            error="insufficient funds",
        )
        assert order.success is False
        assert order.error == "insufficient funds"

    def test_is_mutable(self):
        intent = make_trade_intent()
        order = OrderResult(intent=intent, success=True)
        order.success = False
        assert order.success is False


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: NewMarket
# ═══════════════════════════════════════════════════════════════════════════


class TestNewMarket:
    def test_field_assignment(self):
        nm = NewMarket(
            market_id="0xabc",
            question="Will BTC go up?",
            token_id="tok_yes",
            yes_price=0.55,
            no_price=0.45,
            tags=["crypto", "btc"],
            discovered_at="2025-01-01T00:00:00Z",
        )
        assert nm.market_id == "0xabc"
        assert nm.question == "Will BTC go up?"
        assert nm.token_id == "tok_yes"
        assert nm.yes_price == 0.55
        assert nm.no_price == 0.45
        assert nm.tags == ["crypto", "btc"]
        assert nm.discovered_at == "2025-01-01T00:00:00Z"

    def test_default_factory_fields(self):
        nm = NewMarket(
            market_id="0x1", question="q", token_id="t",
            yes_price=0.5, no_price=0.5,
        )
        assert nm.tags == []
        assert nm.discovered_at == ""

    def test_tags_not_shared_between_instances(self):
        """Default list factory should create independent lists."""
        a = NewMarket(market_id="0x1", question="q", token_id="t", yes_price=0.5, no_price=0.5)
        b = NewMarket(market_id="0x2", question="q", token_id="t", yes_price=0.5, no_price=0.5)
        a.tags.append("crypto")
        assert b.tags == []

    def test_is_mutable(self):
        nm = NewMarket(market_id="0x1", question="q", token_id="t", yes_price=0.5, no_price=0.5)
        nm.yes_price = 0.60
        assert nm.yes_price == 0.60


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: TickContext (frozen)
# ═══════════════════════════════════════════════════════════════════════════


class TestTickContext:
    def test_field_assignment(self):
        ctx = make_tick_context(
            tick_price=68000.0,
            yes_price=0.55,
            state=MarketState.ENTERED,
            entry_price=0.50,
            entry_side="YES",
        )
        assert ctx.tick_price == 68000.0
        assert ctx.yes_price == 0.55
        assert ctx.state is MarketState.ENTERED
        assert ctx.entry_price == 0.50
        assert ctx.entry_side == "YES"

    def test_is_frozen(self):
        ctx = make_tick_context()
        with pytest.raises(FrozenInstanceError):
            ctx.tick_price = 99999.0  # type: ignore[misc]

    def test_factory_defaults(self):
        ctx = make_tick_context()
        assert ctx.tick_price == 67_000.0
        assert ctx.state is MarketState.IDLE
        assert ctx.entry_price is None
        assert ctx.entry_time is None
        assert ctx.entry_side is None
        assert ctx.entry_size_usdc is None

    def test_all_fields_populated(self):
        """Construct with all optional fields set to verify no errors."""
        ctx = make_tick_context(
            state=MarketState.ENTERED,
            entry_price=0.45,
            entry_time=1_700_000_000.0,
            entry_side="NO",
            entry_size_usdc=5.0,
        )
        assert ctx.entry_price == 0.45
        assert ctx.entry_time == 1_700_000_000.0
        assert ctx.entry_side == "NO"
        assert ctx.entry_size_usdc == 5.0

    def test_strategy_config_attached(self):
        cfg = make_strategy_config(edge_threshold=0.10)
        ctx = make_tick_context(strategy_config=cfg)
        assert ctx.strategy_config.entry.min_edge == 0.10


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass smoke tests: TickDecision (frozen)
# ═══════════════════════════════════════════════════════════════════════════


class TestTickDecision:
    def test_default_factory_lists(self):
        td = TickDecision()
        assert td.expired_ids == []
        assert td.exit_decisions == []
        assert td.entry_decisions == []

    def test_is_frozen(self):
        td = TickDecision()
        with pytest.raises(FrozenInstanceError):
            td.expired_ids = ["0x1"]  # type: ignore[misc]

    def test_lists_not_shared_between_instances(self):
        a = TickDecision()
        b = TickDecision()
        # Even though we can't reassign, we verify they are distinct objects
        assert a.expired_ids is not b.expired_ids

    def test_with_populated_fields(self):
        intent = make_trade_intent()
        td = TickDecision(
            expired_ids=["0xdead"],
            entry_decisions=[("0xentry", intent)],
        )
        assert td.expired_ids == ["0xdead"]
        assert len(td.entry_decisions) == 1
        assert td.entry_decisions[0][0] == "0xentry"
        assert td.entry_decisions[0][1] is intent
