"""selector — Opportunity Selector module.

Public API:
    run_selector() -> list[dict]
        Read all data/results/*.json files, score each via LLM (or fallback),
        filter by MIN_COMPOSITE_SCORE, rank descending, and write atomically to
        data/pending_trades.json.  Returns the written candidate list.
"""

from estimator.selector.opportunity_selector import run_selector

__all__ = ["run_selector"]
