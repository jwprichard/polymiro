"""research_agent.py — orchestration loop for a single research run.

Picks the highest-scoring unprocessed Opportunity from data/opportunities.json,
runs the full pipeline (interpret → fetch → graph build → probability estimate),
and writes a PredictionResult to data/results/{market_id}.json.

Exit codes:
    0 — result written successfully, OR no work to do (empty queue, all done)
    1 — pipeline ran but failed to write a result file
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import config
import mirofish
from fetchers import FetcherError, NewsFetcher, WebFetcher, WeatherFetcher, WikiFetcher
from mirofish import MiroFishError
from mirofish.neo4j_query import (
    Neo4jQueryError,
    estimate_probability,
    format_graph_as_context,
    query_graph,
)
from research.query_interpreter import QueryInterpreter
from research.source_router import route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fetcher class name → concrete class mapping
# ---------------------------------------------------------------------------

_FETCHER_CLASSES = {
    "WeatherFetcher": WeatherFetcher,
    "WikiFetcher": WikiFetcher,
    "WebFetcher": WebFetcher,
    "NewsFetcher": NewsFetcher,
}

# ---------------------------------------------------------------------------
# Shared-state file paths
# ---------------------------------------------------------------------------

_OPPORTUNITIES_FILE = config.DATA_DIR / "opportunities.json"
_QUEUE_FILE = config.DATA_DIR / "research_queue.json"


# ---------------------------------------------------------------------------
# Helper: atomic JSON file write
# ---------------------------------------------------------------------------

def _write_json_atomic(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via a .tmp sibling."""
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Helper: load the research queue
# ---------------------------------------------------------------------------

def _load_queue() -> list[str]:
    """Return the list of already-processed market_ids (missing file → [])."""
    if not _QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(item) for item in data]
        logger.warning("research_queue.json has unexpected format; treating as empty.")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read research_queue.json: %s — treating as empty.", exc)
        return []


# ---------------------------------------------------------------------------
# Helper: append market_id to queue atomically
# ---------------------------------------------------------------------------

def _append_to_queue(market_id: str) -> None:
    """Add *market_id* to the research queue file, atomically."""
    current = _load_queue()
    if market_id not in current:
        current.append(market_id)
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_QUEUE_FILE, current)


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

def process_top_opportunity() -> None:
    """Run the full research pipeline for the highest-scoring unprocessed opportunity."""

    # Step 1: Read opportunities.json ----------------------------------------
    if not _OPPORTUNITIES_FILE.exists():
        logger.warning("opportunities.json not found at %s — nothing to do.", _OPPORTUNITIES_FILE)
        sys.exit(0)

    try:
        raw = json.loads(_OPPORTUNITIES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse opportunities.json: %s — nothing to do.", exc)
        sys.exit(0)

    if not raw:
        logger.warning("opportunities.json is empty — nothing to do.")
        sys.exit(0)

    # Step 2: Load research queue --------------------------------------------
    queue: list[str] = _load_queue()

    # Step 3: Select highest-scoring opportunity not already in queue --------
    # Sort descending by opportunity_score; pick first not in queue.
    sorted_opps = sorted(raw, key=lambda o: o.get("opportunity_score", 0.0), reverse=True)

    selected: dict | None = None
    for opp in sorted_opps:
        mid = opp.get("market_id", "")
        if mid not in queue:
            selected = opp
            break

    if selected is None:
        logger.info("All opportunities have already been researched — nothing to do.")
        sys.exit(0)

    market_id: str = selected["market_id"]
    question: str = selected["question"]
    current_yes_price: float = float(selected["current_yes_price"])
    scanned_at: str = selected.get("scanned_at", "")

    logger.info("Processing market %s: %r (score=%.4f)", market_id, question, selected.get("opportunity_score", 0.0))

    # Step 4: Interpret question → FetchPlan ---------------------------------
    fetch_plan = QueryInterpreter().interpret(question)
    logger.info("FetchPlan: topic=%r sources=%r", fetch_plan.topic, fetch_plan.sources)

    # Step 5: Route sources → fetcher class names; instantiate and fetch -----
    fetcher_names: list[str] = route(fetch_plan.sources)
    logger.info("Fetcher class names selected: %s", fetcher_names)

    run_id = market_id  # stable identifier for the fetched-docs subdirectory
    doc_paths: list[Path] = []

    for fetcher_name in fetcher_names:
        fetcher_cls = _FETCHER_CLASSES.get(fetcher_name)
        if fetcher_cls is None:
            logger.warning("Unknown fetcher class name %r — skipping.", fetcher_name)
            continue

        # Resolve the query string: prefer the fetcher-name key in queries,
        # then any matching source-label key, then fall back to fetch_plan.topic.
        query_string: str = fetch_plan.queries.get(fetcher_name, "")
        if not query_string:
            # fetch_plan.queries is keyed by source labels (e.g. "wiki"),
            # so try all source labels whose mapped class name equals this fetcher.
            for src_label, mapped_name in [
                (label, _FETCHER_CLASSES.get(route([label])[0]))
                for label in fetch_plan.sources
            ]:
                if mapped_name is fetcher_cls:
                    query_string = fetch_plan.queries.get(src_label, "")
                    if query_string:
                        break

        if not query_string:
            query_string = fetch_plan.topic

        fetcher = fetcher_cls(run_id=run_id)
        try:
            paths = fetcher.fetch(query_string)
            doc_paths.extend(paths)
            logger.info("%s fetched %d document(s).", fetcher_name, len(paths))
        except FetcherError as exc:
            logger.warning("%s failed: %s — continuing without its documents.", fetcher_name, exc)

    # Extra fetch for the opposing side of race markets ("X before Y")
    side_b_paths: list[Path] = []
    if len(fetch_plan.race_sides) == 2:
        side_b_query = fetch_plan.race_sides[1]
        logger.info("Race market detected — fetching side B: %r", side_b_query)
        fetcher = NewsFetcher(run_id=run_id)
        try:
            side_b_paths = fetcher.fetch(side_b_query)
            doc_paths.extend(side_b_paths)
            logger.info("NewsFetcher (side B) fetched %d document(s).", len(side_b_paths))
        except FetcherError as exc:
            logger.warning("NewsFetcher (side B) failed: %s — continuing.", exc)

    logger.info("Total documents fetched: %d", len(doc_paths))

    # Step 6: Build knowledge graph via MiroFish -----------------------------
    graph_id: str | None = None
    graph_context: str = ""

    try:
        graph_id = mirofish.build_graph(question, doc_paths)
        logger.info("MiroFish graph built: graph_id=%r", graph_id)
    except MiroFishError as exc:
        logger.error("MiroFish graph build failed: %s — continuing with empty graph context.", exc)

    # Step 7: Query the graph ------------------------------------------------
    if graph_id is not None:
        try:
            rows = query_graph(graph_id)
            graph_context = format_graph_as_context(rows)
            logger.info("Graph context produced: %d chars from %d row(s).", len(graph_context), len(rows))
        except Neo4jQueryError as exc:
            logger.error("Neo4j query failed: %s — continuing with empty graph context.", exc)
            graph_context = ""

    # Step 8: Estimate probability -------------------------------------------
    predicted_probability, evidence_summary = estimate_probability(
        graph_context,
        question,
        doc_paths=doc_paths,
        current_yes_price=current_yes_price,
        race_sides=fetch_plan.race_sides or None,
        side_b_doc_paths=side_b_paths,
    )
    logger.info(
        "Probability estimate: %.4f  evidence_summary=%r",
        predicted_probability,
        evidence_summary[:120] if evidence_summary else "",
    )

    # Step 9: Compute edge ---------------------------------------------------
    edge: float = predicted_probability - current_yes_price
    logger.info("Edge: %.4f (predicted=%.4f, market=%.4f)", edge, predicted_probability, current_yes_price)

    # Step 10: Ensure results directory exists --------------------------------
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Step 11: Write result atomically ----------------------------------------
    result: dict = {
        "market_id": market_id,
        "question": question,
        "predicted_probability": predicted_probability,
        "edge": edge,
        "evidence_summary": evidence_summary,
        "graph_id": graph_id,
        "scanned_at": scanned_at,
    }

    tmp_path = config.RESULTS_DIR / f"{market_id}.tmp.json"
    final_path = config.RESULTS_DIR / f"{market_id}.json"

    try:
        tmp_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(tmp_path, final_path)
        logger.info("Result written to %s", final_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write result file %s: %s", final_path, exc)
        sys.exit(1)

    # Step 12: Append to research queue only after successful result write ----
    try:
        _append_to_queue(market_id)
        logger.info("market_id %s added to research queue.", market_id)
    except Exception as exc:  # noqa: BLE001
        # Queue update failure is non-fatal — the result is already persisted.
        logger.warning("Could not update research_queue.json: %s", exc)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    process_top_opportunity()
