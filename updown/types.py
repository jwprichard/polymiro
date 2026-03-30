from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from updown.exit_rules import ExitSignal
    from updown.strategy_config import StrategyConfig


# ---------------------------------------------------------------------------
# Canonical time source
# ---------------------------------------------------------------------------


def get_exchange_now_ms(tick: Optional[PriceUpdate] = None) -> int:
    """Return the canonical exchange timestamp in epoch milliseconds.

    When a Binance *tick* is available, its ``timestamp_ms`` field is the
    single source of truth for all per-tick decision timestamps.  When no
    tick is available (e.g. during startup or REST-only code paths), falls
    back to wall-clock ``time.time()`` so callers always get a usable value.

    Parameters
    ----------
    tick:
        A ``PriceUpdate`` from the Binance trade stream.  May be ``None``
        when called outside a tick-processing context.

    Returns
    -------
    int
        Epoch milliseconds — either from the exchange or wall-clock.
    """
    if tick is not None:
        return tick.timestamp_ms
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# MarketState enum and transition logic
# ---------------------------------------------------------------------------

class MarketState(Enum):
    """Lifecycle state of a market position.

    Transient states ENTERING and EXITING replace the former
    ``pending_order`` boolean, giving the orchestrator an explicit
    state machine to guard against duplicate orders and illegal
    transitions.
    """

    IDLE = "idle"
    ENTERING = "entering"
    ENTERED = "entered"
    EXITING = "exiting"
    COOLDOWN = "cooldown"


class InvalidTransitionError(Exception):
    """Raised when a MarketState transition is not permitted."""


# Legal transitions expressed as {from_state: {to_state, ...}}.
_VALID_TRANSITIONS: dict[MarketState, set[MarketState]] = {
    MarketState.IDLE: {MarketState.ENTERING},
    MarketState.ENTERING: {MarketState.ENTERED, MarketState.IDLE},
    MarketState.ENTERED: {MarketState.EXITING, MarketState.IDLE},
    MarketState.EXITING: {MarketState.COOLDOWN, MarketState.IDLE},
    MarketState.COOLDOWN: {MarketState.IDLE},
}


def validate_transition(current: MarketState, target: MarketState) -> None:
    """Raise ``InvalidTransitionError`` if *current* -> *target* is illegal.

    This is a pure function with no I/O dependencies, suitable for
    direct use in unit tests.
    """
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        allowed_names = ", ".join(sorted(s.name for s in allowed)) or "(none)"
        raise InvalidTransitionError(
            f"Transition {current.name} -> {target.name} is not permitted. "
            f"Allowed transitions from {current.name}: {allowed_names}"
        )


def transition(current: MarketState, target: MarketState) -> MarketState:
    """Return *target* if the transition from *current* is legal.

    Raises ``InvalidTransitionError`` otherwise.  Convenience wrapper
    around ``validate_transition`` that returns the new state so callers
    can write ``state = transition(state, MarketState.ENTERING)``.
    """
    validate_transition(current, target)
    return target


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PriceUpdate:
    """A single Binance BTC/USDT price tick."""

    symbol: str
    price: float
    timestamp_ms: int


@dataclass
class MarketSnapshot:
    """Current state of a Polymarket binary-option market."""

    market_id: str
    question: str
    token_id: str
    yes_price: float
    no_price: float
    spread: float
    timestamp_ms: int


@dataclass
class SignalResult:
    """Output of the momentum signal computation.

    Produced by compute_signal() in updown/signal.py.
    All probabilities and prices are expressed as floats in [0, 1].
    """

    direction: str  # "YES" | "NO"
    implied_probability: float  # momentum-derived probability, clamped [0.01, 0.99]
    market_price: float  # current Polymarket YES price [0, 1]
    edge: float  # implied_probability - market_price (YES) or inverse (NO)
    should_trade: bool  # True when abs(edge) exceeds the configured threshold


@dataclass
class TradeIntent:
    """Pre-execution trade decision, before any order is placed."""

    market_id: str
    token_id: str
    side: str  # "buy" | "sell"
    outcome: str  # "yes" | "no"
    size_usdc: float
    signal: SignalResult
    market: MarketSnapshot
    reason: str
    signal_price: Optional[float] = None  # price at signal time, for slippage check
    tick_timestamp_ms: int = 0  # exchange timestamp of the signal-generating tick


@dataclass
class OrderResult:
    """Post-execution result from the Polymarket CLOB."""

    intent: TradeIntent
    success: bool
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    error: Optional[str] = None
    timestamp_ms: int = 0


@dataclass
class NewMarket:
    """A newly discovered Polymarket market eligible for updown trading."""

    market_id: str
    question: str
    token_id: str
    yes_price: float
    no_price: float
    tags: list[str] = field(default_factory=list)
    discovered_at: str = ""


# ---------------------------------------------------------------------------
# Decision-pipeline types (frozen, purely serializable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickContext:
    """Immutable bundle of every input the decision pipeline needs.

    Contains price data, market metadata, position state, and strategy
    configuration.  Holds no WS/HTTP client references — purely
    serializable so it can be logged, replayed, or passed across threads.
    """

    # -- Tick price data -----------------------------------------------------
    tick_price: float               # latest BTC price from Binance
    tick_timestamp_ms: int          # exchange timestamp of the tick
    open_price: float               # window-open reference price
    yes_price: float                # current Polymarket YES token price
    no_price: float                 # current Polymarket NO token price
    price_age_ms: int               # staleness of the Polymarket price

    # -- Market identification -----------------------------------------------
    market_id: str
    question: str
    token_id: str
    expiry_time: float              # UNIX timestamp when the market expires

    # -- Position lifecycle --------------------------------------------------
    state: MarketState

    # -- Entry details (populated when state >= ENTERED) ---------------------
    entry_price: Optional[float]
    entry_time: Optional[float]     # UNIX timestamp of position open
    entry_side: Optional[str]       # "YES" | "NO"
    entry_size_usdc: Optional[float]

    # -- Strategy configuration ----------------------------------------------
    strategy_config: StrategyConfig


@dataclass(frozen=True)
class TickDecision:
    """Immutable aggregate of all decisions produced for a single tick.

    Separates three categories of action so the orchestrator can process
    them in order: prune expired markets, close positions, then open new
    ones.
    """

    # Condition IDs whose markets have expired and should be pruned.
    expired_ids: list[str] = field(default_factory=list)

    # Positions to close: each entry is (condition_id, ExitSignal, TradeIntent).
    exit_decisions: list[tuple[str, ExitSignal, TradeIntent]] = field(
        default_factory=list,
    )

    # New positions to open: each entry is (condition_id, TradeIntent).
    entry_decisions: list[tuple[str, TradeIntent]] = field(
        default_factory=list,
    )
