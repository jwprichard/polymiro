"""source_router.py: maps FetchPlan source labels to fetcher class name strings.

This module is a pure static lookup table — no Ollama calls, no I/O, no side
effects.  Callers instantiate the returned class names themselves so that
run_id and other constructor arguments remain the caller's responsibility.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Maps source label strings (as produced by QueryInterpreter / FetchPlan) to
# fetcher class name strings.  Class name strings are used rather than direct
# imports so the caller can decide when and how to instantiate each fetcher.
_REGISTRY: dict[str, str] = {
    "wikipedia": "WikiFetcher",
    "wiki": "WikiFetcher",
    "weather": "WeatherFetcher",
    "news_search": "NewsFetcher",
    "news": "NewsFetcher",
    "web_search": "WebFetcher",
    "web": "WebFetcher",
    "crypto_prices": "WebFetcher",  # stub; CryptoFetcher is a future replacement
}

# Used for any label that is not present in _REGISTRY.
_FALLBACK: str = "NewsFetcher"

# Returned when the mapped list would otherwise be empty (i.e. the caller
# passed an empty source list).
_MINIMUM: list[str] = ["WikiFetcher", "WebFetcher"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route(opportunity_sources: list[str]) -> list[str]:
    """Map source label strings to fetcher class name strings.

    Each label in *opportunity_sources* is looked up in ``_REGISTRY``.
    Unrecognised labels are mapped to ``_FALLBACK`` ("NewsFetcher").

    The returned list is deduplicated while preserving the order in which
    class names first appear.  If *opportunity_sources* is empty the minimum
    set ``["WikiFetcher", "WebFetcher"]`` is returned so that callers always
    receive at least two useful fetchers.

    No Ollama calls are made inside this module regardless of the value of
    ``config.LLM_PROVIDER``.

    Examples::

        >>> route(["wikipedia", "weather"])
        ['WikiFetcher', 'WeatherFetcher']

        >>> route(["crypto_prices"])
        ['WebFetcher']

        >>> route([])
        ['WikiFetcher', 'WebFetcher']

        >>> route(["unknown_source"])
        ['NewsFetcher']
    """
    if not opportunity_sources:
        return list(_MINIMUM)

    seen: set[str] = set()
    result: list[str] = []

    for label in opportunity_sources:
        class_name = _REGISTRY.get(label, _FALLBACK)
        if class_name not in seen:
            seen.add(class_name)
            result.append(class_name)

    # result is guaranteed non-empty here because opportunity_sources was
    # non-empty and every label maps to at least _FALLBACK.
    return result
