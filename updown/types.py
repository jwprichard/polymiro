from dataclasses import dataclass, field
from typing import Optional


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
