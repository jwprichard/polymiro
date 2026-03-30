"""QueryInterpreter: converts a prediction-market question into a FetchPlan.

Two code paths:
  - LLM_PROVIDER == "ollama": calls Ollama, falls back to heuristics on error.
  - LLM_PROVIDER == "none"  : uses keyword heuristics only, never raises.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from common import config
from estimator.research._llm_utils import ollama_json_call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FetchPlan:
    """Structured plan describing what to fetch for a given market question."""

    topic: str
    entities: list[str]
    timeframe: str
    sources: list[str]  # e.g. ["wiki", "news_search", "web_search"]
    queries: dict[str, str]  # fetcher_name -> concrete search string
    race_sides: list[str] = field(default_factory=list)  # [side_a, side_b] for "X before Y" markets


# ---------------------------------------------------------------------------
# System / user prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a research planner. Extract key research parameters from a "
    "prediction market question and return a JSON FetchPlan. "
    "Return ONLY a JSON object matching the schema exactly."
)

_USER_TEMPLATE = """\
Question: {question}

Example response:
{{"topic": "Bitcoin price", "entities": ["Bitcoin", "BTC", "cryptocurrency"], "timeframe": "before April 2026", "sources": ["wiki", "web_search"], "queries": {{"wiki": "Bitcoin price history", "web_search": "Bitcoin price prediction 2026"}}}}

Now return a JSON FetchPlan for the question above."""


# ---------------------------------------------------------------------------
# Keyword heuristic helper
# ---------------------------------------------------------------------------

_CRYPTO_KEYWORDS = {"bitcoin", "crypto", "eth", "btc", "ethereum", "solana"}
_WEATHER_KEYWORDS = {"weather", "rain", "hurricane", "temperature", "storm", "flood"}
_ELECTION_KEYWORDS = {"election", "vote", "president", "senate", "congress", "ballot"}
_MARKET_KEYWORDS = {"stock", "market", "nasdaq", "sp500", "fed", "rate", "inflation"}


def _detect_race_sides(question: str) -> list[str]:
    """Return [side_a, side_b] if the question is a 'X before Y' race, else [].

    Handles patterns like:
        "New Rihanna Album before GTA VI?"
        "Will X happen before Y?"
        "Russia-Ukraine Ceasefire before GTA VI?"
    """
    if "before" not in question.lower():
        return []
    parts = re.split(r'\bbefore\b', question, flags=re.IGNORECASE, maxsplit=1)
    if len(parts) != 2:
        return []
    # Strip leading fluff from side A ("Will ", "New ", "A ", question marks)
    side_a = re.sub(r'^(will\s+|new\s+|a\s+|an\s+)', '', parts[0].strip(), flags=re.IGNORECASE).strip("? ")
    side_b = parts[1].strip().strip("? ")
    if side_a and side_b:
        return [side_a, side_b]
    return []


def _keyword_plan(question: str) -> FetchPlan:
    """Return a FetchPlan derived purely from keyword matching."""
    lower = question.lower()
    words = lower.split()
    first_word = words[0].strip("?.,!") if words else "topic"

    # Determine sources and topic via keyword groups.
    if any(kw in lower for kw in _CRYPTO_KEYWORDS):
        sources = ["wiki", "web_search"]
        topic = first_word
    elif any(kw in lower for kw in _WEATHER_KEYWORDS):
        sources = ["weather", "wiki"]
        # Attempt a very simple location extraction: take the first capitalised
        # token from the original question that is not a stop word.
        location = _extract_location(question)
        topic = location if location else "global"
    elif any(kw in lower for kw in _ELECTION_KEYWORDS):
        sources = ["wiki", "news_search", "web_search"]
        topic = first_word
    elif any(kw in lower for kw in _MARKET_KEYWORDS):
        sources = ["wiki", "web_search"]
        topic = first_word
    else:
        sources = ["wiki", "news_search"]
        topic = first_word

    # Build a simple search string per source.
    short_q = question[:120]  # keep search strings concise
    queries: dict[str, str] = {src: f"{topic} {short_q}" for src in sources}

    race_sides = _detect_race_sides(question)
    if race_sides:
        queries["news_search_side_b"] = race_sides[1]

    return FetchPlan(
        topic=topic,
        entities=_extract_entities(question),
        timeframe="",
        sources=sources,
        queries=queries,
        race_sides=race_sides,
    )


def _extract_location(question: str) -> str:
    """Return the first capitalised word that is not a common stop word."""
    stop_words = {
        "Will", "The", "A", "An", "Is", "Are", "Does", "Do", "Has",
        "Have", "Can", "Could", "Would", "Should", "Might", "May",
        "What", "When", "Where", "Who", "Which", "How", "Why",
    }
    for token in question.split():
        clean = token.strip("?.,!\"'()")
        if clean and clean[0].isupper() and clean not in stop_words:
            return clean
    return ""


def _extract_entities(question: str) -> list[str]:
    """Return capitalised tokens as naive entity candidates."""
    stop_words = {
        "Will", "The", "A", "An", "Is", "Are", "Does", "Do", "Has",
        "Have", "Can", "Could", "Would", "Should", "Might", "May",
        "What", "When", "Where", "Who", "Which", "How", "Why",
        "Before", "After", "By", "In", "On", "At", "To", "Of",
        "For", "With", "Over", "Under", "Than",
    }
    entities: list[str] = []
    for token in question.split():
        clean = token.strip("?.,!\"'()")
        if clean and clean[0].isupper() and clean not in stop_words:
            entities.append(clean)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique or ["unknown"]


# ---------------------------------------------------------------------------
# FetchPlan builder from raw LLM dict
# ---------------------------------------------------------------------------

def _dict_to_fetch_plan(data: dict, question: str) -> FetchPlan:
    """Validate and coerce a raw dict into a FetchPlan.

    Missing or malformed fields are filled with sensible defaults so that
    callers always receive a fully populated FetchPlan.
    """
    topic = str(data.get("topic", question[:60]))
    entities = list(data.get("entities", []))
    timeframe = str(data.get("timeframe", ""))
    sources = list(data.get("sources", ["wiki", "news_search"]))

    raw_queries = data.get("queries", {})
    if not isinstance(raw_queries, dict):
        raw_queries = {}
    queries: dict[str, str] = {str(k): str(v) for k, v in raw_queries.items()}

    # Fill in any sources that are missing a query string.
    for src in sources:
        if src not in queries:
            queries[src] = f"{topic} {question[:80]}"

    race_sides = _detect_race_sides(question)
    if race_sides:
        queries["news_search_side_b"] = race_sides[1]

    return FetchPlan(
        topic=topic,
        entities=entities,
        timeframe=timeframe,
        sources=sources,
        queries=queries,
        race_sides=race_sides,
    )


# ---------------------------------------------------------------------------
# Main interpreter class
# ---------------------------------------------------------------------------


class QueryInterpreter:
    """Converts a prediction-market question into a FetchPlan.

    This class never raises to its caller regardless of code path.
    """

    def interpret(self, market_question: str) -> FetchPlan:  # noqa: PLR0911
        """Return a FetchPlan for *market_question*.

        Falls back to keyword heuristics on any failure so the caller always
        receives a valid FetchPlan.
        """
        if config.LLM_PROVIDER != "ollama":
            return _keyword_plan(market_question)

        try:
            return self._interpret_with_llm(market_question)
        except Exception:  # noqa: BLE001
            logger.warning(
                "LLM interpretation failed for %r — falling back to heuristics.",
                market_question,
            )
            return _keyword_plan(market_question)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _interpret_with_llm(self, market_question: str) -> FetchPlan:
        """Call Ollama and parse the response into a FetchPlan."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(question=market_question),
            },
        ]

        raw: dict = ollama_json_call(
            messages=messages,
            model=config.OLLAMA_MODEL,
        )

        return _dict_to_fetch_plan(raw, market_question)
