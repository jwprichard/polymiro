#!/usr/bin/env python3
"""End-to-end pipeline smoke test. Run with: LLM_PROVIDER=none python3 scripts/smoke_test_research.py"""
import os
import sys
import json
import subprocess
import pathlib

os.environ.setdefault("LLM_PROVIDER", "none")

# Add project root to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

from common import config  # noqa: E402

PASS = "PASS"
FAIL_PREFIX = "FAIL"
results = []

_first_result: dict | None = None  # shared across checks 3–5


def check(name, fn):
    try:
        fn()
        results.append((name, True, None))
        print(f"PASS: {name}")
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"FAIL: {name} — {e}")


# ---------------------------------------------------------------------------
# Check 1: data/opportunities.json is non-empty and parseable
# ---------------------------------------------------------------------------

def _check_opportunities_file():
    opp_file = config.ESTIMATOR_DATA_DIR / "opportunities.json"
    assert opp_file.exists(), f"opportunities.json not found at {opp_file}"
    raw = opp_file.read_text(encoding="utf-8")
    assert raw.strip(), "opportunities.json is empty"
    data = json.loads(raw)
    assert isinstance(data, list) and len(data) > 0, (
        f"Expected non-empty list, got {type(data).__name__} "
        f"with {len(data) if isinstance(data, list) else 'N/A'} entries"
    )


check("data/opportunities.json is non-empty and parseable", _check_opportunities_file)

# ---------------------------------------------------------------------------
# Check 2: process_top_opportunity() runs without raising
# ---------------------------------------------------------------------------

def _check_process_top_opportunity():
    # Import here so LLM_PROVIDER=none is already set when the module loads.
    from estimator.research.research_agent import process_top_opportunity  # noqa: PLC0415

    # process_top_opportunity() calls sys.exit(0) when it finishes successfully
    # (or when there is nothing left to do).  We catch SystemExit and treat
    # exit code 0 as success; any other exit code or an unhandled exception is
    # a failure.
    try:
        process_top_opportunity()
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
        assert code == 0, f"process_top_opportunity() exited with code {code}"


check("process_top_opportunity() runs without raising", _check_process_top_opportunity)

# ---------------------------------------------------------------------------
# Check 3: A result file exists with all required fields
# ---------------------------------------------------------------------------

_REQUIRED_RESULT_FIELDS = [
    "market_id",
    "question",
    "predicted_probability",
    "edge",
    "evidence_summary",
    "graph_id",
    "scanned_at",
]


def _check_result_file_exists_and_valid():
    global _first_result
    result_files = sorted(config.RESULTS_DIR.glob("*.json"))
    assert result_files, f"No .json result files found in {config.RESULTS_DIR}"

    first_file = result_files[0]
    data = json.loads(first_file.read_text(encoding="utf-8"))
    missing = [f for f in _REQUIRED_RESULT_FIELDS if f not in data]
    assert not missing, f"Result file {first_file.name} missing fields: {missing}"

    _first_result = data  # cache for subsequent checks


check("A result file exists with all required fields", _check_result_file_exists_and_valid)

# ---------------------------------------------------------------------------
# Check 4: predicted_probability is a float in [0.0, 1.0]
# ---------------------------------------------------------------------------

def _check_predicted_probability():
    assert _first_result is not None, "No result loaded — check 3 must pass first"
    prob = _first_result["predicted_probability"]
    assert isinstance(prob, float), (
        f"predicted_probability is {type(prob).__name__}, expected float"
    )
    assert 0.0 <= prob <= 1.0, (
        f"predicted_probability {prob} is outside [0.0, 1.0]"
    )


check("predicted_probability is a float in [0.0, 1.0]", _check_predicted_probability)

# ---------------------------------------------------------------------------
# Check 5: edge equals predicted_probability − current_yes_price (±1e-9)
# ---------------------------------------------------------------------------

def _check_edge_calculation():
    assert _first_result is not None, "No result loaded — check 3 must pass first"

    market_id = _first_result["market_id"]
    predicted_probability = _first_result["predicted_probability"]
    stored_edge = _first_result["edge"]

    opp_file = config.ESTIMATOR_DATA_DIR / "opportunities.json"
    opportunities = json.loads(opp_file.read_text(encoding="utf-8"))

    matching = [o for o in opportunities if o.get("market_id") == market_id]
    assert matching, (
        f"market_id {market_id!r} not found in opportunities.json"
    )
    opportunity = matching[0]

    current_yes_price = float(opportunity["current_yes_price"])
    expected_edge = predicted_probability - current_yes_price

    diff = abs(stored_edge - expected_edge)
    assert diff < 1e-9, (
        f"edge mismatch: stored={stored_edge}, "
        f"expected={expected_edge} (diff={diff})"
    )


check(
    "edge equals predicted_probability - current_yes_price (within 1e-9)",
    _check_edge_calculation,
)

# ---------------------------------------------------------------------------
# Check 6: data/research_queue.json contains the processed market_id
# ---------------------------------------------------------------------------

def _check_queue_contains_market_id():
    assert _first_result is not None, "No result loaded — check 3 must pass first"

    market_id = _first_result["market_id"]
    queue_file = config.ESTIMATOR_DATA_DIR / "research_queue.json"
    assert queue_file.exists(), f"research_queue.json not found at {queue_file}"

    queue_data = json.loads(queue_file.read_text(encoding="utf-8"))
    assert isinstance(queue_data, list), (
        f"research_queue.json should be a list, got {type(queue_data).__name__}"
    )
    assert market_id in queue_data, (
        f"market_id {market_id!r} not found in research_queue.json "
        f"(queue contains {len(queue_data)} entries)"
    )


check(
    "data/research_queue.json contains the processed market_id",
    _check_queue_contains_market_id,
)

# ---------------------------------------------------------------------------
# Check 7: No executable call to /api/simulation/start exists under mirofish/
#
# The bridge.py file contains the string "/api/simulation/start" inside
# docstrings and comments (which is expected — it explicitly documents that
# the endpoint must never be called).  The real constraint is that no actual
# HTTP invocation of that path exists in the source.  We therefore search for
# a requests/session .post() call whose argument contains "simulation/start",
# which would indicate an actual invocation rather than a documentation note.
# ---------------------------------------------------------------------------

def _check_no_simulation_start_call():
    mirofish_dir = str(pathlib.Path(__file__).parent.parent / "mirofish")

    # Pattern matches actual HTTP call patterns like:
    #   .post("…/api/simulation/start…")  or  requests.post("…simulation/start…")
    # It does NOT match plain comments or docstring references.
    proc = subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.py",
            r"\.post\s*([^)]*simulation/start",
            mirofish_dir,
        ],
        capture_output=True,
        text=True,
    )
    # grep exits 1 when no match is found (the desired outcome).
    # Any matching lines mean an actual call was found.
    assert proc.returncode == 1 or proc.stdout.strip() == "", (
        f"Found executable call(s) to /api/simulation/start in mirofish/:\n"
        f"{proc.stdout.strip()}"
    )


check(
    "No executable call to /api/simulation/start exists in mirofish/",
    _check_no_simulation_start_call,
)

# ---------------------------------------------------------------------------
# Final summary and exit
# ---------------------------------------------------------------------------

passed_count = sum(1 for _, passed, _ in results if passed)
print(f"\n{passed_count}/{len(results)} checks passed")

if any(not passed for _, passed, _ in results):
    sys.exit(1)

sys.exit(0)
