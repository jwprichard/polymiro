from dataclasses import dataclass
from typing import Optional


@dataclass
class Market:
    market_id: str
    question: str
    token_id: str
    yes_price: float
    no_price: float
    volume_24h: float
    closes_at: Optional[str]
    is_active: bool


@dataclass
class Opportunity:
    market_id: str
    question: str
    current_yes_price: float
    current_no_price: float
    volume_24h: float
    spread: float
    closes_at: Optional[str]
    opportunity_score: float
    data_sources_suggested: list[str]
    scanned_at: str
