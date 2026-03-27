"""
Neo4j query layer — raw graph extraction and context formatting.

Provides two public functions:
    query_graph(graph_id)         -> list[dict]   raw Cypher records
    format_graph_as_context(rows) -> str          human-readable, weight-sorted, capped

NOTE: estimate_probability() will be added in Task 12.
"""

from __future__ import annotations

from pathlib import Path

from neo4j import GraphDatabase

import config
from research._llm_utils import ollama_json_call

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------

class Neo4jQueryError(RuntimeError):
    """Raised on Neo4j connection or query failure."""


# ---------------------------------------------------------------------------
# Cypher query
# ---------------------------------------------------------------------------

_GRAPH_QUERY = (
    "MATCH (e:Entity {graph_id: $graph_id})-[r]->(e2:Entity) "
    "RETURN e.name, e.type, type(r), r.weight, e2.name, e2.type "
    "LIMIT 200"
)

# Hard cap on formatted context output (characters)
_CONTEXT_CHAR_LIMIT = 6000


def query_graph(graph_id: str) -> list[dict]:
    """
    Run the canonical relationship query for *graph_id* against Neo4j.

    Returns a list of dicts with keys matching the RETURN clause:
        e.name, e.type, type(r), r.weight, e2.name, e2.type

    Raises Neo4jQueryError on connection or query failure.
    Driver is always closed in the finally block.
    """
    driver = None
    try:
        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
        with driver.session() as session:
            result = session.run(_GRAPH_QUERY, graph_id=graph_id)
            # Materialise all records before the session closes.
            rows = [dict(record) for record in result]
        return rows
    except Neo4jQueryError:
        raise
    except Exception as exc:
        raise Neo4jQueryError(
            f"Neo4j query failed for graph_id={graph_id!r}: {exc}"
        ) from exc
    finally:
        if driver is not None:
            driver.close()


# ---------------------------------------------------------------------------
# Context formatter
# ---------------------------------------------------------------------------

def format_graph_as_context(rows: list[dict]) -> str:
    """
    Convert raw Cypher records to a human-readable relationship string.

    Each line:
        {e.name} ({e.type}) --[{rel_type} w={weight}]--> {e2.name} ({e2.type})

    Rows are sorted by r.weight descending (None treated as 0).
    Output is capped at 6 000 characters; if truncated, appends:
        \\n... [truncated]

    Returns an empty string for an empty row list.
    """
    if not rows:
        return ""

    # Sort most-strongly-weighted relationships first; None weight -> 0.
    sorted_rows = sorted(
        rows,
        key=lambda r: (r.get("r.weight") or 0),
        reverse=True,
    )

    lines: list[str] = []
    for row in sorted_rows:
        src_name = row.get("e.name") or ""
        src_type = row.get("e.type") or ""
        rel_type = row.get("type(r)") or ""
        weight   = row.get("r.weight")
        dst_name = row.get("e2.name") or ""
        dst_type = row.get("e2.type") or ""

        weight_str = str(weight) if weight is not None else "None"
        line = (
            f"{src_name} ({src_type}) "
            f"--[{rel_type} w={weight_str}]--> "
            f"{dst_name} ({dst_type})"
        )
        lines.append(line)

    output = "\n".join(lines)

    if len(output) > _CONTEXT_CHAR_LIMIT:
        output = output[:_CONTEXT_CHAR_LIMIT] + "\n... [truncated]"

    return output


# ---------------------------------------------------------------------------
# Probability estimator
# ---------------------------------------------------------------------------

_DOC_READ_LIMIT = 1000       # chars per file when falling back to raw docs
_DOC_TOTAL_LIMIT = 6000      # total chars for concatenated doc fallback
_PROMPT_CONTEXT_LIMIT = 5000 # chars passed inside the user message


def estimate_probability(
    graph_context: str,
    market_question: str,
    node_count: int = 0,
    edge_count: int = 0,
    doc_paths: list[Path] | None = None,
    current_yes_price: float | None = None,
) -> tuple[float, str]:
    """Estimate the YES probability for *market_question* using the LLM.

    Parameters
    ----------
    graph_context:
        Pre-formatted graph context string from format_graph_as_context().
        May be empty when the graph build failed or returned no rows.
    market_question:
        The full Polymarket market question string.
    node_count, edge_count:
        Informational counts — not used in the prompt but reserved for
        future prompt enrichment.
    doc_paths:
        Optional list of raw fetched-doc Paths.  Used as a fallback when
        *graph_context* is empty.
    current_yes_price:
        The current Polymarket YES price (0.0–1.0).  When provided, it is
        included in the prompt as a calibration anchor so the LLM knows the
        market consensus before reasoning from evidence.

    Returns
    -------
    (probability, reasoning) — always.  Never raises to the caller.
    """
    if config.LLM_PROVIDER == "none":
        return (0.5, "LLM disabled — neutral estimate")

    try:
        # Build effective context -----------------------------------------
        if not graph_context and doc_paths:
            parts: list[str] = []
            for p in doc_paths:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    parts.append(text[:_DOC_READ_LIMIT])
                except Exception:  # noqa: BLE001
                    continue
            concatenated = "\n\n".join(parts)
            effective_context = concatenated[:_DOC_TOTAL_LIMIT]
        else:
            effective_context = graph_context

        # Build messages ---------------------------------------------------
        evidence_block = (
            effective_context[:_PROMPT_CONTEXT_LIMIT]
            if effective_context
            else "No evidence available."
        )

        if current_yes_price is not None:
            price_line = (
                f"Current market YES price: {current_yes_price:.3f} "
                f"(this is the crowd consensus — only deviate significantly "
                f"if the evidence strongly warrants it)\n\n"
            )
        else:
            price_line = ""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a prediction market analyst. "
                    "Respond with only a JSON object — no markdown, no prose."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Market question: {market_question}\n\n"
                    f"{price_line}"
                    f"Evidence:\n{evidence_block}\n\n"
                    'Respond with JSON: {"probability": <float 0.0 to 1.0>,'
                    ' "reasoning": "<2-3 sentences>"}'
                ),
            },
        ]

        # Call LLM ---------------------------------------------------------
        result = ollama_json_call(messages, model=config.OLLAMA_MODEL, max_retries=2)

        raw_prob = float(result["probability"])
        probability = max(0.0, min(1.0, raw_prob))
        reasoning = result.get("reasoning", "")
        return (probability, reasoning)

    except ValueError:
        return (0.5, "LLM parse error — defaulting to neutral")
    except Exception:  # noqa: BLE001
        return (0.5, "LLM error — defaulting to neutral")


# ---------------------------------------------------------------------------
# Smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rows = query_graph("test_graph_id_does_not_exist")
    print(f"rows: {len(rows)}")
    ctx = format_graph_as_context(rows)
    print(f"context length: {len(ctx)}")
