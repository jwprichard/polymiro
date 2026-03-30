# Draft Plan — Software Architect

## Domain Focus
State machine design, concern separation in `_process_tick`, time source standardization, and pure-function extraction to enable testability and future backtesting.

## Proposed Tasks

### Task 1: Define MarketState enum and transition rules
**Priority:** high
**Depends on:** none
**Files likely affected:** updown/types.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit, Bash
**Success criteria:**
- MarketState enum exists with states IDLE, ENTERED, EXITING, COOLDOWN
- A transition table or validator function enforces legal transitions (e.g. IDLE->ENTERED, ENTERED->EXITING, EXITING->COOLDOWN, COOLDOWN->IDLE)
- Invalid transitions raise a descriptive error rather than silently succeeding
- Enum and transition logic are fully unit-testable with no I/O dependencies

### Task 2: Refactor TrackedMarket to use MarketState enum
**Priority:** high
**Depends on:** Task 1
**Files likely affected:** updown/loop.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit, Bash, Grep
**Success criteria:**
- TrackedMarket.state field replaces the traded (bool), pending_order (bool), and last_trade_time (float) fields
- has_open_position property derives from state == ENTERED (not from boolean flags)
- Cooldown is expressed as COOLDOWN state with a cooldown_until timestamp, replacing the inline `now - last_trade_time < _COOLDOWN_SECONDS` check
- No remaining references to tracked.traded or tracked.pending_order anywhere in updown/

### Task 3: Extract pure decision functions from _process_tick
**Priority:** high
**Depends on:** Task 2
**Files likely affected:** updown/loop.py, updown/decisions.py (new file)
**Agent:** @Backend Architect
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- A new `updown/decisions.py` module contains pure functions: `evaluate_expiry()`, `evaluate_exit()`, `evaluate_entry()`
- `evaluate_expiry(tracked_markets, now)` returns a list of condition_ids to prune -- no side effects
- `evaluate_exit(tracked, position_price, strategy_config, now)` returns Optional[ExitSignal] -- calls existing check_exit() but handles the price-side resolution and staleness gating as pure logic, returning a decision object rather than executing
- `evaluate_entry(tracked, yes_price, no_price, btc_current, btc_open, threshold, now)` returns Optional[TradeIntent] -- calls existing compute_signal() and builds intent, no execution
- Each function accepts only plain data (floats, strings, dataclasses) -- no WS client, no aiohttp session
- All three functions can be called from a test harness with synthetic data

### Task 4: Create decision result types for tick processing
**Priority:** high
**Depends on:** none
**Files likely affected:** updown/types.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- A `TickDecision` dataclass (or similar) aggregates the outputs of the three evaluate functions into a single structure: expired_ids, exit_decisions (list of condition_id + ExitSignal + TradeIntent pairs), entry_decisions (list of condition_id + TradeIntent pairs)
- The type is frozen/immutable so the decision layer produces a value that the execution layer consumes without mutation
- The type is importable from updown/types.py alongside existing types

### Task 5: Rewrite _process_tick as thin orchestrator over pure functions
**Priority:** high
**Depends on:** Task 3, Task 4
**Files likely affected:** updown/loop.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit, Bash, Grep
**Success criteria:**
- _process_tick is reduced to roughly 50-80 lines that: (1) gather current prices from WS clients, (2) call the pure decision functions, (3) execute side effects (place_order, state transitions, rotation)
- The early-rotation logic remains in _process_tick (it requires async HTTP) but is clearly separated from signal/decision logic
- State transitions on TrackedMarket use the MarketState enum transitions from Task 1
- No behavioral change: dry-run and live trading produce identical outcomes to the current implementation

### Task 6: Standardize time sources to prefer exchange timestamps
**Priority:** medium
**Depends on:** Task 2
**Files likely affected:** updown/loop.py, updown/decisions.py, updown/executor.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit, Grep
**Success criteria:**
- A `get_now_ms()` utility is introduced that prefers exchange-sourced timestamps (tick.timestamp_ms from Binance, or Polymarket WS timestamp) and falls back to `int(time.time() * 1000)` when unavailable
- _process_tick passes exchange-derived `now` to all decision functions rather than calling `time.time()` inline
- TrackedMarket.discovered_at, entry_time, and cooldown_until use exchange-derived timestamps where possible
- The executor's `now_ms = int(time.time() * 1000)` on line 257 is replaced with a parameter or the shared utility
- Existing `time.time()` calls in `_seed_markets_from_rest` (wall-clock for slug computation) are preserved -- those correctly need wall-clock time

### Task 7: Validate refactor preserves behavior via dry-run smoke test
**Priority:** medium
**Depends on:** Task 5, Task 6
**Files likely affected:** none (runtime validation)
**Agent:** @Backend Architect
**Allowed tools:** Bash, Read, Grep
**Success criteria:**
- `UPDOWN_DRY_MODE=true python -m updown` starts without import errors or crashes
- Heartbeat logs appear showing tracked markets and price updates
- Signal evaluation logs appear when BTC price moves
- Dry-mode trade logs appear when edge exceeds threshold
- Exit rule evaluation logs appear for open (dry) positions
- No tracebacks in the first 60 seconds of operation

## Constraints & Decisions

**Constraint: No new dependencies.** The refactor uses only stdlib and existing project dependencies. The new `decisions.py` module imports only from `updown/types.py`, `updown/signal.py`, `updown/exit_rules.py`, and `updown/strategy_config.py`.

**Constraint: Atomic refactor per task.** Each task should leave the system in a runnable state. Task 2 (TrackedMarket refactor) must update all references in loop.py in the same change -- no intermediate broken state where old boolean fields are removed but callers still reference them.

**Risk: Early rotation logic entanglement.** The early-rotation block (lines 677-699) mixes time checks with async HTTP calls. It cannot be fully extracted into a pure function. The plan keeps it in `_process_tick` but isolates it from the signal/decision path.

**Risk: pending_order as concurrency guard.** The `pending_order` flag currently prevents re-entrant order submission while an async `place_order` is in flight. The EXITING state in the new enum must preserve this guard semantics -- the state machine must not allow ENTERED->ENTERED transitions while an order is pending. This may require a transient ENTERING state or keeping the pending guard as a separate concern from the lifecycle state.

**Decision: decisions.py as a new file.** Extracting pure functions into a separate module (rather than keeping them as private methods in loop.py) provides a clear import boundary that enforces no-I/O discipline. Test files import from `updown.decisions` without needing to mock any WS or HTTP infrastructure.

## Open Decisions

1. **Should ENTERING and EXITING be explicit states, or should pending_order remain a separate flag?** The `pending_order` flag guards against concurrent order submission during the async `place_order` call. It could be modeled as a transient sub-state (ENTERING, EXITING) or kept as an orthogonal boolean on TrackedMarket. The former is cleaner but adds two more states; the latter is simpler but partially defeats the purpose of the state machine. The user should decide which approach to take.

2. **Should cooldown_until be an absolute timestamp or should COOLDOWN state carry a duration?** An absolute timestamp (`cooldown_until = now + _COOLDOWN_SECONDS`) is simpler for the transition check (`now >= cooldown_until` -> transition to IDLE). A duration requires storing the entry time into cooldown. Recommend absolute timestamp but flagging for confirmation.

3. **Should the `TickDecision` type include rotation decisions?** Rotation requires async HTTP and is inherently side-effectful. The current plan excludes it from the pure decision layer. If the Backend Architect team wants rotation to be replay-able in backtesting, it would need a different approach (pre-computed rotation schedule). Confirm whether rotation is in-scope for the pure extraction.

4. **What is the canonical "now" for a tick?** The Binance tick carries `timestamp_ms`. The Polymarket WS updates have their own timestamps. Wall-clock `time.time()` is a third source. For backtesting, all three must come from the same synthetic clock. The user should confirm whether the Binance tick timestamp should be the single source of truth for all per-tick decisions.

## Notes

The current `_process_tick` has five conceptual phases that map cleanly to the proposed extraction:

1. **Expiry pruning** (lines 664-670) -> `evaluate_expiry()` pure function
2. **Rotation** (lines 672-699) -> stays in `_process_tick` (async I/O required)
3. **Exit monitoring** (lines 701-828) -> `evaluate_exit()` pure function + execution layer
4. **Cooldown gating + signal** (lines 831-896) -> `evaluate_entry()` pure function
5. **Order execution** (lines 938-973) -> execution layer in `_process_tick`

The existing `compute_signal()` and `check_exit()` are already pure. The new decision functions are thin wrappers that add price resolution, staleness checks, and cooldown gating -- logic that is currently interleaved with WS client calls in `_process_tick`.

The state machine transition diagram:

```
IDLE --[entry signal + order success]--> ENTERED
ENTERED --[exit signal + order success]--> COOLDOWN
ENTERED --[market expires]--> IDLE (position lost to expiry)
COOLDOWN --[cooldown_until reached]--> IDLE
IDLE --[market expires]--> (removed from tracking)
```

If transient states are adopted (Open Decision 1):

```
IDLE --> ENTERING --> ENTERED --> EXITING --> COOLDOWN --> IDLE
                 \-> IDLE (order failed)       \-> IDLE (if allow_reentry=false, skip COOLDOWN)
```
