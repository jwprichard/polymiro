"""Opportunity Selector — ranks PredictionResult files into pending_trades.json.

Usage:
    python -m selector.opportunity_selector
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from utils.io import write_json_atomic
from research._llm_utils import ollama_json_call

logger = logging.getLogger(__name__)


class SelectorError(Exception):
    """Raised when the selector encounters an unrecoverable error."""


def _load_results() -> list[dict]:
    """Read all JSON files from RESULTS_DIR; skip unparseable files with a warning."""
    results_dir = config.RESULTS_DIR
    if not results_dir.exists():
        logger.warning("Results directory %s does not exist; returning empty list.", results_dir)
        return []

    loaded = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
            record = json.loads(text)
            loaded.append(record)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("Skipping %s — failed to parse: %s", path.name, exc)
    return loaded


def _score_with_ollama(question: str, evidence_summary: str) -> dict:
    """Ask the LLM to rate confidence in the edge given the evidence.

    Returns a dict with at least ``confidence`` (float) and ``rationale`` (str).
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a prediction market analyst. "
                "Given a market question and an evidence summary, "
                "rate your confidence that the edge (mispricing) is genuine. "
                "Respond with a JSON object containing exactly two fields: "
                '"confidence" (a float between 0.0 and 1.0) and '
                '"rationale" (a brief string explaining your reasoning).'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Market question: {question}\n\n"
                f"Evidence summary: {evidence_summary}\n\n"
                "Please provide your confidence assessment as JSON."
            ),
        },
    ]
    return ollama_json_call(messages=messages, model=config.OLLAMA_MODEL)


def _fallback_confidence(evidence_summary: str) -> float:
    """Heuristic confidence when LLM_PROVIDER == 'none'.

    Scales linearly with evidence length up to 500 characters, capped at 1.0.
    """
    return min(len(evidence_summary) / 500.0, 1.0)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def run_selector() -> list[dict]:
    """Main entry point: score, filter, rank, and persist trade candidates.

    Returns the list of candidates written to pending_trades.json.
    """
    results = _load_results()
    logger.info("Loaded %d result file(s) from %s.", len(results), config.RESULTS_DIR)

    ranked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates = []

    for record in results:
        market_id = record.get("market_id", "<unknown>")
        edge = record.get("edge")

        if edge is None:
            logger.debug("Skipping %s — no 'edge' field.", market_id)
            continue

        try:
            edge = float(edge)
        except (TypeError, ValueError):
            logger.warning("Skipping %s — 'edge' is not numeric: %r", market_id, edge)
            continue

        question = record.get("question", "")
        evidence_summary = record.get("evidence_summary", "")

        # Determine confidence via LLM or fallback.
        if config.LLM_PROVIDER == "none":
            confidence = _fallback_confidence(evidence_summary)
            rationale = "fallback: length-based heuristic"
        else:
            try:
                llm_response = _score_with_ollama(question, evidence_summary)
                raw_confidence = llm_response.get("confidence")
                if raw_confidence is None:
                    raise ValueError("LLM response missing 'confidence' field")
                confidence = _clamp(float(raw_confidence))
                rationale = str(llm_response.get("rationale", ""))
            except Exception as exc:
                logger.warning(
                    "LLM call failed for %s (%s); using fallback confidence.", market_id, exc
                )
                confidence = _fallback_confidence(evidence_summary)
                rationale = f"fallback (LLM error): {exc}"

        composite_score = abs(edge) * confidence
        direction = "YES" if edge > 0 else "NO"

        if composite_score < config.MIN_COMPOSITE_SCORE:
            logger.debug(
                "Excluding %s — composite_score %.4f < threshold %.4f.",
                market_id,
                composite_score,
                config.MIN_COMPOSITE_SCORE,
            )
            continue

        candidate = {
            "market_id": market_id,
            "question": question,
            "edge": edge,
            "confidence": round(confidence, 6),
            "composite_score": round(composite_score, 6),
            "predicted_probability": record.get("predicted_probability"),
            "current_yes_price": record.get("current_yes_price"),
            "direction": direction,
            "ranked_at": ranked_at,
        }
        candidates.append(candidate)
        logger.debug(
            "Candidate %s: edge=%.4f confidence=%.4f composite=%.4f direction=%s rationale=%s",
            market_id,
            edge,
            confidence,
            composite_score,
            direction,
            rationale,
        )

    # Rank descending by composite_score.
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    write_json_atomic(config.PENDING_TRADES_FILE, candidates)
    logger.info(
        "Wrote %d candidate(s) to %s.", len(candidates), config.PENDING_TRADES_FILE
    )

    return candidates


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = run_selector()
    print(f"Selector complete — {len(results)} candidate(s) written to pending_trades.json.")
    for c in results:
        print(
            f"  {c['direction']:3s}  edge={c['edge']:+.4f}  conf={c['confidence']:.3f}"
            f"  composite={c['composite_score']:.4f}  {c['question'][:70]}"
        )
