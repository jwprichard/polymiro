"""
opportunity_scorer.py — weighted scoring formula for Polymarket opportunities.

Scores a market on four dimensions:
  - Spread (price inefficiency signal)
  - Liquidity (inverse of 24h volume — low liquidity = more mispricing potential)
  - Urgency (proximity of close date)
  - Fetchability (how well external data sources can inform the question)

All sub-scores are clamped to [0.0, 1.0] before weighting.
Final score is in [0.0, 1.0], rounded to 4 decimal places.
"""

import json
import math
from datetime import datetime, timezone
from typing import Optional

import config
from scanner.models import Market

# ---------------------------------------------------------------------------
# Module-level weight constants
# ---------------------------------------------------------------------------
WEIGHT_SPREAD: float = 0.35
WEIGHT_LIQUIDITY: float = 0.20
WEIGHT_URGENCY: float = 0.20
WEIGHT_FETCHABILITY: float = 0.25

SPREAD_CAP: float = 0.20

# Fixed source vocabulary for _classify_topic
_SOURCE_VOCAB: list[str] = [
    "crypto_prices",
    "news_search",
    "weather",
    "sports_scores",
    "polling_data",
    "on_chain_data",
    "macro_data",
    "wikipedia",
    "web_search",
]

# Default fallback returned on any _classify_topic failure
_FALLBACK_SOURCES: tuple[float, list[str]] = (0.5, ["news_search", "web_search"])


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def days_until_close(market: Market) -> float:
    """Return float days from UTC now until market.closes_at.

    Returns 365.0 when closes_at is None.  The returned value is NOT floored
    here; the urgency formula enforces the 0.5-day floor via max().
    """
    if market.closes_at is None:
        return 365.0

    closes_str = market.closes_at.strip()

    # Normalize 'Z' suffix to '+00:00' so fromisoformat works on Python < 3.11
    if closes_str.endswith("Z"):
        closes_str = closes_str[:-1] + "+00:00"

    close_dt = datetime.fromisoformat(closes_str)

    # Ensure the datetime is timezone-aware
    if close_dt.tzinfo is None:
        close_dt = close_dt.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    delta_seconds = (close_dt - now).total_seconds()
    return delta_seconds / 86400.0


def _classify_topic_none_mode(question: str) -> tuple[float, list[str]]:
    """Keyword heuristics — no external I/O."""
    q = question.lower()

    if any(kw in q for kw in ("bitcoin", "btc", "eth", "ethereum", "crypto", "solana", "token")):
        return 0.5, ["crypto_prices", "news_search"]

    if any(kw in q for kw in ("rain", "storm", "hurricane", "flood", "weather", "temperature")):
        return 0.5, ["weather", "news_search"]

    if any(kw in q for kw in ("election", "poll", "vote", "president", "senate", "congress")):
        return 0.5, ["polling_data", "news_search"]

    if any(kw in q for kw in ("score", "win", "championship", "nfl", "nba", "mlb", "fifa", "league")):
        return 0.5, ["sports_scores", "news_search"]

    return 0.5, ["news_search", "web_search"]


def _classify_topic_ollama(question: str) -> tuple[float, list[str]]:
    """Ask the local Ollama model to score fetchability and suggest sources.

    Falls back to _FALLBACK_SOURCES on any error (import, connection, timeout,
    bad JSON, missing keys, out-of-range values).
    """
    try:
        import ollama  # type: ignore[import]
    except ImportError:
        return _FALLBACK_SOURCES

    prompt = (
        "You are a data-source routing assistant for a prediction-market research pipeline.\n\n"
        f'Market question: "{question}"\n\n'
        "Available source labels: "
        + json.dumps(_SOURCE_VOCAB)
        + "\n\n"
        "Respond with ONLY valid JSON in this exact format (no explanation, no markdown):\n"
        '{"fetchability": <float 0.0-1.0>, "sources": [<label>, ...]}\n\n'
        "fetchability: how easy is it to find real-world data that would help answer this question?\n"
        "sources: a subset of the available labels that apply."
    )

    try:
        response = ollama.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
            timeout=10,
        )
        raw_text: str = response["message"]["content"].strip()

        # Strip optional markdown code fences
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        parsed = json.loads(raw_text)
        fetchability: float = float(parsed["fetchability"])
        sources: list[str] = [s for s in parsed["sources"] if s in _SOURCE_VOCAB]

        if not (0.0 <= fetchability <= 1.0):
            return _FALLBACK_SOURCES
        if not sources:
            sources = ["news_search", "web_search"]

        return fetchability, sources

    except Exception:  # noqa: BLE001 — intentional broad catch for robustness
        return _FALLBACK_SOURCES


def _classify_topic(question: str) -> tuple[float, list[str]]:
    """Route to the correct classification strategy based on config.LLM_PROVIDER."""
    if config.LLM_PROVIDER == "none":
        return _classify_topic_none_mode(question)
    # Default to Ollama path for any other provider value including "ollama"
    return _classify_topic_ollama(question)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_opportunity(market: Market, spread: float) -> tuple[float, list[str]]:
    """Score a market opportunity on a [0.0, 1.0] scale.

    Parameters
    ----------
    market:
        A populated Market dataclass instance.
    spread:
        Absolute difference between our estimated probability and the current
        Polymarket YES price (e.g. 0.12 means a 12-percentage-point edge).

    Returns
    -------
    (score, data_sources_suggested)
        score — weighted composite, rounded to 4 decimal places.
        data_sources_suggested — list of source label strings.
    """
    # -- Spread sub-score --
    spread_score = min(spread / SPREAD_CAP, 1.0)

    # -- Liquidity sub-score (inverse log; +2 prevents log(0) and log(1)==0) --
    liquidity_score = min(1.0 / math.log(market.volume_24h + 2), 1.0)

    # -- Urgency sub-score (closer close date = higher urgency = higher score) --
    urgency_score = min(1.0 / max(days_until_close(market), 0.5), 1.0)

    # -- Fetchability sub-score (via LLM or keyword heuristics) --
    fetchability_score, source_labels = _classify_topic(market.question)
    fetchability_score = min(max(fetchability_score, 0.0), 1.0)

    # -- Weighted composite --
    score = (
        WEIGHT_SPREAD * spread_score
        + WEIGHT_LIQUIDITY * liquidity_score
        + WEIGHT_URGENCY * urgency_score
        + WEIGHT_FETCHABILITY * fetchability_score
    )

    return round(score, 4), source_labels
