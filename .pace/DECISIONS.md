# Decisions

## BTC 5-min market discovery filter
**Decided:** 2026-03-29T16:30:00Z
**Choice:** Question string match — match on keywords like 'Bitcoin' + 'Up' + 'Down' + '5' in the market question text
**Affects:** Task 4 (Polymarket WS client), Task 7 (orchestrator loop)

## CLOB client approach
**Decided:** 2026-03-29T16:30:00Z
**Choice:** Use py-clob-client library (official Polymarket Python lib) for auth, signing, and order construction
**Affects:** Task 6 (trade executor)

## Order type strategy
**Decided:** 2026-03-29T16:30:00Z
**Choice:** Aggressive limit orders (cross the spread) for v1 to guarantee fills. Optimize pricing strategy in later iteration.
**Affects:** Task 6 (trade executor), Task 7 (orchestrator loop)

## SCALE_FACTOR default value
**Decided:** 2026-03-29T21:10:00Z
**Choice:** 0.01 — requires ~0.03% BTC move for edge=0.05. Roughly 3x less sensitive than the previous 0.003.
**Affects:** Task 2 (SCALE_FACTOR config)

## Minimum BTC % change gate default
**Decided:** 2026-03-29T21:10:00Z
**Choice:** 0.01% (0.0001 as decimal) — filters out near-zero noise ticks entirely.
**Affects:** Task 3 (min-change gate)

## Strategy file path configuration
**Decided:** 2026-03-29
**Choice:** CLI arg `--strategy` passed to `python main.py updown --strategy path/to/strategy.yml`
**Affects:** Strategy loader module, CLI parser in main.py

## Re-entry after exit
**Decided:** 2026-03-29
**Choice:** Configurable via strategy.yml, default to no re-entry. Add `allow_reentry: false` in exit_rules.
**Affects:** Exit integration in tick processor, TrackedMarket state

## Strategy schema scope
**Decided:** 2026-03-29
**Choice:** Define the full strategy.yml schema skeleton (all sections from the example), but only implement exit_rules for now.
**Affects:** Strategy loader/schema definition

## Missing strategy.yml behavior
**Decided:** 2026-03-29
**Choice:** Fail fast — refuse to start the updown loop without a strategy file.
**Affects:** Strategy loader, updown loop startup

## Exit delta convention
**Decided:** 2026-03-29
**Choice:** Positive numbers in YAML (e.g. `max_loss_delta: 0.10`), code negates internally for comparison.
**Affects:** Exit evaluator logic

## NO-side price source
**Decided:** 2026-03-29
**Choice:** Use direct NO price from WS client when available, fall back to `1.0 - yes_price`.
**Affects:** Exit evaluator, position tracking

## Default slippage tolerance
**Decided:** 2026-03-30
**Choice:** 0.01 (1 cent) — matches typical Polymarket tick size
**Affects:** Task 1 (slippage protection)

## Exit trade slippage tolerance
**Decided:** 2026-03-30
**Choice:** 2x wider for exits (0.02) — prioritizes closing positions over price precision
**Affects:** Task 1 (slippage protection — exit orders use double the entry tolerance)

## ENTERING/EXITING transient states
**Decided:** 2026-03-30
**Choice:** Use transient states. MarketState enum: IDLE → ENTERING → ENTERED → EXITING → COOLDOWN → IDLE (6 states). Fully replaces all boolean flags.
**Affects:** Tasks 1, 3, 5, 6

## Canonical tick timestamp source
**Decided:** 2026-03-30
**Choice:** Binance tick.timestamp_ms is the single source of truth for all per-tick decisions. Wall-clock for Polymarket price age and non-decision operations.
**Affects:** Tasks 4, 7, 8

## Cooldown representation
**Decided:** 2026-03-30
**Choice:** Absolute timestamp. cooldown_until = now + COOLDOWN_SECONDS. Transition: now >= cooldown_until → IDLE.
**Affects:** Tasks 1, 3

## Rotation in pure decision layer
**Decided:** 2026-03-30
**Choice:** Exclude. Rotation stays in _process_tick as async I/O. Replay skips rotation.
**Affects:** Tasks 5, 6, 9

## Tick log rotation policy
**Decided:** 2026-03-30
**Choice:** Daily rotation. New file per day: updown_ticks_YYYY-MM-DD.jsonl.
**Affects:** Task 8

## Backtest output granularity
**Decided:** 2026-03-30
**Choice:** Trade events only (entries/exits). Compact output for P&L analysis.
**Affects:** Task 9

## Replay open_price fidelity
**Decided:** 2026-03-30
**Choice:** Use tick log value only. No rolling window reconstruction.
**Affects:** Task 9

## Percent threshold semantics
**Decided:** 2026-03-30
**Choice:** Percentage of entry_price (e.g. 2% of $0.60 entry = $0.012 delta)
**Affects:** Tasks 2, 4

## allow_reentry location (updated)
**Decided:** 2026-03-30
**Choice:** Under `risk` section (risk.allow_reentry)
**Affects:** Tasks 1, 2, 3

## Required vs optional sections
**Decided:** 2026-03-30
**Choice:** All active sections required — fail-fast if any is missing
**Affects:** Task 2

## Strategy file organization
**Decided:** 2026-03-30
**Choice:** Multiple YAML files in `updown/strategies/`, pick one at startup
**Affects:** Tasks 1, 2, 5

## Async test tooling
**Decided:** 2026-03-30
**Choice:** Use pytest-asyncio with function-scoped event loops
**Affects:** Task 1 (conftest), Task 9, Task 10, Task 13

## py_clob_client in test environment
**Decided:** 2026-03-30
**Choice:** Ensure py_clob_client is installed as a dev dependency
**Affects:** Task 7

## Coverage threshold
**Decided:** 2026-03-30
**Choice:** Enforce 80% line coverage via pytest-cov
**Affects:** Task 14

## Existing test_polymarket_ws.py disposition
**Decided:** 2026-03-30
**Choice:** Rewrite to match the current async aiohttp production API
**Affects:** Task 10

## StrategyConfig fixture source
**Decided:** 2026-03-30
**Choice:** Both: in-code factory for unit tests + real_strategy_config fixture loading from YAML for smoke tests
**Affects:** Task 1, Task 6
