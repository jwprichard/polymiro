"""
smoke_test.py — End-to-end smoke test for the Polymarket scanner pipeline.

Runs one scan cycle, validates the output schema, prints the top 3
opportunities, and verifies the atomic-write guarantee.

Usage:
    python scripts/smoke_test.py

Exit codes:
    0 — all checks passed
    1 — polymarket-cli unavailable OR schema validation errors found
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path regardless of where this script is
# invoked from.
sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402 — must come after sys.path insert
from scanner.polymarket_client import PolymarketClientError  # noqa: E402
from scanner.scanner_agent import run_scan  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema specification
# ---------------------------------------------------------------------------

# Each entry is (field_name, expected_types).
# closes_at is optional (may be absent or null) and is validated separately.
_REQUIRED_FIELDS: list[tuple[str, tuple]] = [
    ("market_id", (str,)),
    ("question", (str,)),
    ("current_yes_price", (float, int)),
    ("current_no_price", (float, int)),
    ("volume_24h", (float, int)),
    ("spread", (float, int)),
    ("opportunity_score", (float, int)),
    ("data_sources_suggested", (list,)),
    ("scanned_at", (str,)),
]


def _validate_entry(entry: dict) -> str | None:
    """Validate a single opportunity dict against the required schema.

    Returns the name of the first failing field, or None if all fields pass.
    """
    for field, expected_types in _REQUIRED_FIELDS:
        if field not in entry:
            return field
        if not isinstance(entry[field], expected_types):
            return field

    # closes_at is optional but, when present, must be str or None/null.
    if "closes_at" in entry:
        val = entry["closes_at"]
        if val is not None and not isinstance(val, str):
            return "closes_at"

    return None


# ---------------------------------------------------------------------------
# Main routine
# ---------------------------------------------------------------------------

def main() -> int:
    """Run all smoke-test checks.  Returns exit code (0 = pass, 1 = fail)."""
    exit_code = 0

    # ------------------------------------------------------------------
    # 1. Run one full scan
    # ------------------------------------------------------------------
    print("--- Running scan ---")
    try:
        opportunities = run_scan()
    except PolymarketClientError as exc:
        print(
            f"ERROR: polymarket-cli is unavailable or returned an error: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Scan complete: {len(opportunities)} opportunities returned.\n")

    # ------------------------------------------------------------------
    # 2. Validate data/opportunities.json schema
    # ------------------------------------------------------------------
    print("--- Validating schema ---")
    out_path = config.DATA_DIR / "opportunities.json"

    try:
        with open(out_path, encoding="utf-8") as fh:
            entries: list[dict] = json.load(fh)
    except FileNotFoundError:
        print(f"SCHEMA ERROR: {out_path} not found after scan.", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"SCHEMA ERROR: could not parse JSON — {exc}", file=sys.stderr)
        return 1

    schema_errors = 0
    for idx, entry in enumerate(entries):
        bad_field = _validate_entry(entry)
        if bad_field is None:
            print(f"  [{idx}] SCHEMA OK")
        else:
            print(f"  [{idx}] SCHEMA ERROR: {bad_field}")
            schema_errors += 1

    if schema_errors:
        print(f"\n{schema_errors} schema error(s) found.")
        exit_code = 1
    else:
        print("All entries passed schema validation.")

    # ------------------------------------------------------------------
    # 3. Print top 3 opportunities
    # ------------------------------------------------------------------
    print("\n--- Top 3 opportunities ---")
    top3 = entries[:3]
    if not top3:
        print("  (no opportunities found)")
    else:
        for rank, entry in enumerate(top3, start=1):
            question = entry.get("question", "")
            truncated = question[:80] + ("..." if len(question) > 80 else "")
            print(
                f"  #{rank}  market_id          : {entry.get('market_id')}\n"
                f"       question           : {truncated}\n"
                f"       opportunity_score  : {entry.get('opportunity_score')}\n"
                f"       data_sources       : {entry.get('data_sources_suggested')}\n"
            )

    # ------------------------------------------------------------------
    # 4. Verify atomic write (no tmp file left behind)
    # ------------------------------------------------------------------
    print("--- Checking atomic write ---")
    tmp_path = config.DATA_DIR / "opportunities.tmp.json"
    if tmp_path.exists():
        print("ATOMIC WRITE FAILED: tmp file still present")
        exit_code = 1
    else:
        print("ATOMIC WRITE OK")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n--- Smoke test {'PASSED' if exit_code == 0 else 'FAILED'} ---")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
