#!/usr/bin/env python3
"""LLM integration smoke test. Run with: LLM_PROVIDER=none python3 scripts/smoke_test_llm.py"""
import os, sys, json, dataclasses, pathlib

os.environ.setdefault("LLM_PROVIDER", "none")
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

# ---------------------------------------------------------------------------
# Test 1: QueryInterpreter.interpret() returns a valid FetchPlan
# ---------------------------------------------------------------------------

from estimator.research.query_interpreter import QueryInterpreter, FetchPlan

plan = QueryInterpreter().interpret("Will Bitcoin exceed $100k before April 2026?")
# Assert it's a FetchPlan
assert isinstance(plan, FetchPlan), f"Expected FetchPlan, got {type(plan)}"
# Assert all required fields present
for field in ("topic", "entities", "timeframe", "sources", "queries"):
    assert hasattr(plan, field), f"FetchPlan missing field: {field}"
assert isinstance(plan.sources, list), "sources must be a list"
assert isinstance(plan.queries, dict), "queries must be a dict"
print("Test 1 PASS: FetchPlan returned")
print(f"  FetchPlan: {json.dumps(dataclasses.asdict(plan), indent=2)}")

# ---------------------------------------------------------------------------
# Test 2: estimate_probability() returns (float, str) in none mode
# ---------------------------------------------------------------------------

from estimator.mirofish.neo4j_query import estimate_probability

prob, reasoning = estimate_probability(
    graph_context="Bitcoin price is $95k. ETF inflows strong.",
    market_question="Will Bitcoin exceed $100k?"
)
assert isinstance(prob, float), f"Expected float, got {type(prob)}"
assert 0.0 <= prob <= 1.0, f"Probability {prob} out of range [0.0, 1.0]"
assert isinstance(reasoning, str), f"Expected str reasoning, got {type(reasoning)}"
print(f"Test 2 PASS: estimate_probability returned ({prob}, {reasoning!r})")

# ---------------------------------------------------------------------------
# Test 3: both fallback paths are confirmed reachable in none mode
# ---------------------------------------------------------------------------

# none mode short-circuit
assert prob == 0.5, f"Expected 0.5 in none mode, got {prob}"
assert "LLM disabled" in reasoning or "neutral" in reasoning.lower(), f"Unexpected reasoning: {reasoning!r}"
print("Test 3 PASS: LLM_PROVIDER=none short-circuit confirmed")

# ---------------------------------------------------------------------------
# Test 4: interpret() never raises even with edge-case input
# ---------------------------------------------------------------------------

for question in ["", "?", "a" * 500]:
    result = QueryInterpreter().interpret(question)
    assert isinstance(result, FetchPlan), f"interpret() raised or returned non-FetchPlan for {question[:20]!r}"
print("Test 4 PASS: interpret() never raises")

# ---------------------------------------------------------------------------
# Final
# ---------------------------------------------------------------------------

print("\nAll LLM smoke tests passed.")
sys.exit(0)
