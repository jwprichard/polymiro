# PLAN: Updown orchestrator state machine refactor, pure-function extraction, and replay infrastructure
_Created: 2026-03-30T14:30:00Z_

## Objective
Refactor the updown orchestrator to use a MarketState enum, extract pure decision functions from `_process_tick` for independent testability, standardize on exchange timestamps, add tick logging and a synchronous replay harness for backtesting, and instrument tick-to-trade latency.

## Tasks

### Task 1: Define MarketState enum and transition rules
**Agent:** @Backend Architect
**Depends on:** none
**Files:** updown/types.py
**Allowed tools:** Read, Edit, Bash
**Success criteria:**
- MarketState enum exists with 6 states: IDLE, ENTERING, ENTERED, EXITING, COOLDOWN (transient ENTERING/EXITING replace pending_order boolean)
- A transition table or validator function enforces legal transitions (IDLE→ENTERING, ENTERING→ENTERED, ENTERING→IDLE on failure, ENTERED→EXITING, EXITING→COOLDOWN, EXITING→IDLE on allow_reentry=false, COOLDOWN→IDLE when now >= cooldown_until, ENTERED→IDLE on expiry)
- Cooldown uses absolute timestamp: cooldown_until = now + COOLDOWN_SECONDS
- Invalid transitions raise a descriptive error rather than silently succeeding
- Enum and transition logic are fully unit-testable with no I/O dependencies

### Task 2: Define TickContext and TickDecision data types
**Agent:** @Backend Architect
**Depends on:** none
**Files:** updown/types.py
**Allowed tools:** Read, Edit
**Success criteria:**
- `TickContext` frozen dataclass bundles all decision-pipeline inputs: tick price/timestamp, open_price, yes_price, no_price, price_age_ms, market_id, question, token_id, expiry_time, current MarketState, entry_price, entry_time, entry_side, entry_size_usdc, strategy_config -- no WS or HTTP client references, purely serializable
- `TickDecision` frozen dataclass aggregates decision outputs: expired_ids (list), exit_decisions (list of condition_id + ExitSignal + TradeIntent tuples), entry_decisions (list of condition_id + TradeIntent tuples)
- Both types are importable from updown/types.py alongside existing types

### Task 3: Refactor TrackedMarket to use MarketState enum
**Agent:** @Backend Architect
**Depends on:** 1
**Files:** updown/loop.py
**Allowed tools:** Read, Edit, Bash, Grep
**Success criteria:**
- TrackedMarket.state field replaces the traded (bool), pending_order (bool), and last_trade_time (float) fields
- has_open_position property derives from state == ENTERED (not from boolean flags)
- Cooldown is expressed as COOLDOWN state with a cooldown_until timestamp, replacing the inline `now - last_trade_time < _COOLDOWN_SECONDS` check
- No remaining references to tracked.traded or tracked.pending_order anywhere in updown/
- System starts and runs without errors after this change (no broken intermediate state)

### Task 4: Standardize time sources to prefer exchange timestamps
**Agent:** @Backend Architect
**Depends on:** 3
**Files:** updown/loop.py, updown/executor.py, updown/polymarket_ws.py, updown/binance_ws.py, updown/types.py
**Allowed tools:** Read, Edit, Grep
**Success criteria:**
- Binance tick.timestamp_ms is the single canonical time source for all per-tick decisions. A `get_exchange_now_ms(tick)` utility extracts it, falling back to `int(time.time() * 1000)` only when no tick is available
- `_process_tick` passes exchange-derived `now` to all decision functions rather than calling `time.time()` inline
- TrackedMarket.discovered_at, entry_time, and cooldown_until use exchange-derived timestamps; discovered_at accepts an explicit timestamp parameter instead of defaulting to `time.time()` at construction
- executor.py `_persist_trade` records the tick's exchange timestamp as `exchange_timestamp_ms` alongside the existing `timestamp_utc` wall-clock field
- polymarket_ws.py continues to use wall-clock time for price age tracking (Polymarket WS timestamps are undocumented/unstable)
- Wall-clock `time.time()` is preserved for non-decision uses: heartbeat intervals, log formatting, TTL display, and `_seed_markets_from_rest` slug computation
- No behavioral change in live or dry-run mode

### Task 5: Extract pure decision functions from _process_tick
**Agent:** @Backend Architect
**Depends on:** 3, 4
**Files:** updown/decisions.py (new), updown/loop.py
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- New module `updown/decisions.py` contains pure functions: `evaluate_expiry()`, `evaluate_exit()`, `evaluate_entry()`
- `evaluate_expiry(tracked_markets, now)` returns a list of condition_ids to prune -- no side effects
- `evaluate_exit(tracked, position_price, strategy_config, now)` returns Optional[ExitSignal] -- handles price-side resolution and staleness gating as pure logic, returning a decision object rather than executing
- `evaluate_entry(tracked, yes_price, no_price, btc_current, btc_open, threshold, now)` returns Optional[TradeIntent] -- calls existing compute_signal() and builds intent, no execution
- Each function accepts only plain data (floats, strings, dataclasses, TickContext) -- no WS client, no aiohttp session
- All three functions can be called from a test harness with synthetic data
- `updown/decisions.py` imports only from `updown/types.py`, `updown/signal.py`, `updown/exit_rules.py`, and `updown/strategy_config.py`

### Task 6: Rewrite _process_tick as thin orchestrator over pure functions
**Agent:** @Backend Architect
**Depends on:** 2, 5
**Files:** updown/loop.py
**Allowed tools:** Read, Edit, Bash, Grep
**Success criteria:**
- `_process_tick` is reduced to roughly 50-80 lines that: (1) gather current prices from WS clients and assemble TickContext, (2) call pure decision functions producing TickDecision, (3) execute side effects (place_order, state transitions, rotation)
- Early-rotation logic remains in `_process_tick` (requires async HTTP) but is clearly separated from signal/decision logic
- State transitions on TrackedMarket use MarketState enum transitions from Task 1
- No behavioral change: dry-run and live trading produce identical outcomes to the current implementation

### Task 7: Instrument tick-to-trade latency tracking
**Agent:** @Backend Architect
**Depends on:** 4, 6
**Files:** updown/loop.py, updown/executor.py, updown/types.py
**Allowed tools:** Read, Edit
**Success criteria:**
- `TradeIntent` gains a `tick_timestamp_ms: int` field capturing the exchange timestamp of the signal-generating tick
- `place_order()` computes `tick_to_order_latency_ms = int(time.time() * 1000) - intent.tick_timestamp_ms` immediately before order submission (or dry-run logging)
- Every order attempt logs latency at INFO level: `[LATENCY] tick_to_order=%dms`
- `_persist_trade` records `tick_to_order_latency_ms` in the trade JSON record
- Heartbeat block computes and logs rolling latency stats: `avg_latency=%dms max_latency=%dms orders=%d` appended to the binance heartbeat format; stats reset each heartbeat interval

### Task 8: Build tick log writer for recording replayable tick streams
**Agent:** @Backend Architect
**Depends on:** 2, 6
**Files:** updown/tick_log.py (new), updown/loop.py, config.py
**Allowed tools:** Read, Write, Edit
**Success criteria:**
- New module `updown/tick_log.py` provides `TickLogger` class with `log_tick(tick_context: TickContext)` method
- Each tick written as a single JSON line (JSONL) with daily rotation: `data/updown_ticks_YYYY-MM-DD.jsonl`
- Record includes: timestamp_ms, price, open_price, yes_price, no_price, price_age_ms, market_id, token_id, expiry_time -- everything needed to replay through the decision pipeline
- `UPDOWN_TICK_LOG_ENABLED` (default false) controls activation; zero overhead when disabled
- Writer uses atomic append (open "a" mode, write line, flush)
- Tick logging called from `_process_tick` after TickContext assembly but before decision evaluation
- Live and dry-run behavior unchanged when tick logging disabled

### Task 9: Build synchronous replay harness and CLI entry point
**Agent:** @Backend Architect
**Depends on:** 5, 8
**Files:** updown/replay.py (new), main.py
**Allowed tools:** Read, Write, Edit, Bash
**Success criteria:**
- New module `updown/replay.py` provides `ReplayEngine` class
- `ReplayEngine.load(path)` reads JSONL tick logs or JSON array files (auto-detects by first character)
- Also accepts existing `updown_trades.json` format with a warning that trade-file replay has lower fidelity (no open_price, no NO price, no price_age_ms)
- `ReplayEngine.run()` iterates ticks synchronously, calling the pure decision functions from `updown/decisions.py`, maintaining simulated position state
- `ReplayEngine.summary()` returns aggregate stats: total ticks, signals generated, trades entered/exited, win/loss count, total P&L, max drawdown
- No asyncio, no network calls during replay -- pure computation over pre-recorded data
- Output includes trade events only (entries/exits), not every tick evaluation
- Uses tick log open_price values directly (no rolling window reconstruction)
- CLI: `python main.py backtest --file <path>` invokes the replay harness; accepts `--strategy`, `--edge-threshold`, optional `--output results.json`; prints summary as formatted JSON to stdout; exit code 0 on success, 1 on error

### Task 10: Validate refactor preserves behavior via dry-run smoke test
**Agent:** @Backend Architect
**Depends on:** 7, 8, 9
**Files:** none (runtime validation)
**Allowed tools:** Bash, Read, Grep
**Success criteria:**
- `UPDOWN_DRY_MODE=true python -m updown` starts without import errors or crashes
- Heartbeat logs appear showing tracked markets, price updates, and latency stats
- Signal evaluation logs appear when BTC price moves
- Dry-mode trade logs appear when edge exceeds threshold, including `[LATENCY]` lines
- Exit rule evaluation logs appear for open (dry) positions
- No tracebacks in the first 60 seconds of operation

## Notes
- **Agent substitution:** The backend-architect draft assigned Task 2 (TickContext) to @Software Architect. Reassigned to @Backend Architect per synthesis instructions (backend-only refactor, single agent).
- **Constraint: No new dependencies.** The refactor uses only stdlib and existing project dependencies. `decisions.py` imports only from updown internals.
- **Constraint: Atomic refactor per task.** Each task leaves the system in a runnable state. Task 3 (TrackedMarket refactor) must update all references in loop.py in the same change.
- **Risk: pending_order as concurrency guard.** The `pending_order` flag prevents re-entrant order submission during async `place_order`. The EXITING state must preserve this guard. May require transient ENTERING/EXITING states or keeping pending as orthogonal concern -- see Open Decision 1.
- **Risk: Early rotation entanglement.** The rotation block (lines 677-699) mixes time checks with async HTTP. Cannot be fully extracted into pure functions; stays in `_process_tick` but isolated from decision path.
- **Risk: compute_signal() module-level constants.** `compute_signal()` reads `config.UPDOWN_SCALE_FACTOR` and `config.UPDOWN_MIN_BTC_PCT_CHANGE` at module load time. For replay, these must be configurable per-run. The replay harness should either monkey-patch or the function should accept parameters.
- **Merged duplicate: Time source standardization.** Both drafts proposed time-source work. Merged into Task 4 taking the most detailed criteria from each.
- **Merged duplicate: Canonical "now" and Polymarket timestamp questions.** Both drafts raised variants of this. Combined into Open Decision 2.
- **Scope preserved:** Neither draft proposed changes outside the `updown/` directory (plus main.py for CLI). No scope creep detected.
- **No gaps identified:** All requirements (state machine, pure extraction, time standardization, replay, latency) are covered.

### Open Decisions
All resolved — see `.pace/DECISIONS.md` for the full record.
