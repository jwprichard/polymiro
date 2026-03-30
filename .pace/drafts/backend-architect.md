# Draft Plan — Backend Architect

## Domain Focus
Data infrastructure for backtesting replay, time-source standardization, and latency instrumentation across the updown trading module.

## Proposed Tasks

### Task 1: Standardize time sources — prefer exchange timestamps over time.time()
**Priority:** high
**Depends on:** none
**Files likely affected:** updown/loop.py, updown/executor.py, updown/polymarket_ws.py, updown/binance_ws.py, updown/types.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit, Grep
**Success criteria:**
- `_process_tick` uses `tick.timestamp_ms` (Binance exchange time) instead of `time.time()` for all decision-critical timestamps (expiry checks, cooldown checks, entry_time recording)
- `TrackedMarket.discovered_at`, `TrackedMarket.entry_time`, and `TrackedMarket.last_trade_time` store exchange-originated millisecond timestamps (int) rather than `time.time()` floats
- `executor.py` `_persist_trade` records the tick's exchange timestamp alongside the existing `timestamp_utc` wall-clock field (new field: `exchange_timestamp_ms`)
- `polymarket_ws.py` `_handle_price_change` and `_handle_book` use the event's server timestamp when available, falling back to `time.time()` only when the event carries no timestamp
- `binance_ws.py` `_now_ms()` helper remains for non-critical uses (heartbeats, logging) but is never used for trade-critical timing
- Wall-clock `time.time()` is still acceptable for heartbeat intervals, log formatting, and TTL display — only decision paths must use exchange time
- All existing tests (if any) continue to pass; no behavioral change in live/dry-run mode

### Task 2: Define TickContext dataclass for pure decision function input
**Priority:** high
**Depends on:** none
**Files likely affected:** updown/types.py
**Agent:** @Software Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- New `TickContext` dataclass in `types.py` bundles all inputs needed by the decision pipeline: `tick: PriceUpdate`, `open_price: float`, `yes_price: float`, `no_price: float`, `price_age_ms: int`, `market_id: str`, `question: str`, `token_id: str`, `expiry_time: float`, `traded: bool`, `last_trade_time: float`, `has_open_position: bool`, `entry_price: Optional[float]`, `entry_time: Optional[float]`, `entry_side: Optional[str]`, `entry_size_usdc: Optional[float]`, `strategy_config: Optional[object]`
- Dataclass is frozen (immutable) so replay cannot accidentally mutate state
- No WS or HTTP client references in the dataclass — purely serializable data

### Task 3: Build tick log writer for recording replayable tick streams
**Priority:** high
**Depends on:** none
**Files likely affected:** updown/tick_log.py (new), updown/loop.py, config.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Write, Edit
**Success criteria:**
- New module `updown/tick_log.py` provides `TickLogger` class with `log_tick(tick_context: TickContext)` method
- Each tick is written as a single JSON line (JSONL format) to `data/updown_ticks.jsonl` for append-friendly, streamable I/O
- Record includes: `tick.timestamp_ms`, `tick.price`, `open_price`, `yes_price`, `no_price`, `price_age_ms`, `market_id`, `token_id`, `expiry_time` — everything needed to replay through the decision pipeline
- New config var `UPDOWN_TICK_LOG_ENABLED` (default `false`) controls whether tick logging is active — zero overhead when disabled
- New config var `UPDOWN_TICK_LOG_FILE` (default `data/updown_ticks.jsonl`) sets the output path
- Writer uses atomic append (open in `"a"` mode, write line, flush) — no file locking needed for single-writer JSONL
- Tick logging is called from `_process_tick` in `loop.py` after the TickContext is assembled but before decision evaluation
- Live and dry-run trading behavior is unchanged when tick logging is disabled

### Task 4: Build synchronous replay harness
**Priority:** high
**Depends on:** 2, 3
**Files likely affected:** updown/replay.py (new)
**Agent:** @Backend Architect
**Allowed tools:** Read, Write, Edit
**Success criteria:**
- New module `updown/replay.py` provides `ReplayEngine` class
- `ReplayEngine.load(path: str)` reads a JSONL tick log or a JSON array file (auto-detects format by first character `[` vs `{`)
- `ReplayEngine.run()` iterates ticks synchronously, calling `compute_signal()` and `check_exit()` for each tick, maintaining simulated position state
- Simulated position state tracks: `entry_price`, `entry_time`, `entry_side`, `open_position` — mirrors `TrackedMarket` but without WS dependencies
- Each simulated trade decision is recorded in a results list with: tick timestamp, signal direction, edge, implied probability, market prices, action taken (buy/sell/hold), simulated P&L
- `ReplayEngine.summary()` returns aggregate stats: total ticks, signals generated, trades entered, trades exited, win/loss count, total P&L, max drawdown
- The engine accepts strategy config (same `StrategyConfig` object) and edge threshold as constructor parameters
- No asyncio, no network calls, no file writes during replay — pure computation over pre-recorded data
- Handles edge cases: empty file, single tick, missing fields in tick records (skip with warning)

### Task 5: Add CLI entry point for backtesting
**Priority:** medium
**Depends on:** 4
**Files likely affected:** main.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- New subcommand `python main.py backtest --file data/updown_ticks.jsonl` invokes the replay harness
- Accepts `--strategy` flag (default `strategy.yml`) for exit rules config, same as `updown` subcommand
- Accepts `--edge-threshold` flag (default from config) for signal threshold override
- Prints replay summary to stdout as formatted JSON
- Accepts optional `--output results.json` flag to write per-tick decision log to a file
- Returns exit code 0 on success, 1 on error (file not found, parse error)
- Help text is consistent with existing subcommand style

### Task 6: Instrument tick-to-trade latency tracking
**Priority:** high
**Depends on:** 1
**Files likely affected:** updown/loop.py, updown/executor.py, updown/types.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- `TradeIntent` gains a new field `tick_timestamp_ms: int` capturing the exchange timestamp of the tick that generated the signal
- `place_order()` computes `tick_to_order_latency_ms = int(time.time() * 1000) - intent.tick_timestamp_ms` immediately before order submission (or dry-run logging)
- Every order attempt logs tick-to-trade latency: `[LATENCY] tick_to_order=%dms` at INFO level
- `_persist_trade` records the latency value as `tick_to_order_latency_ms` in the trade JSON record
- For exit trades, the same latency field is populated using the tick that triggered the exit evaluation

### Task 7: Add latency metrics to heartbeat logs
**Priority:** medium
**Depends on:** 6
**Files likely affected:** updown/loop.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- `_tick_processor` heartbeat block computes and logs rolling latency stats over the heartbeat window
- New fields tracked between heartbeats: `orders_placed` count, `sum_latency_ms`, `max_latency_ms` for orders attempted since last heartbeat
- Heartbeat log line includes: `avg_latency=%dms max_latency=%dms orders=%d` appended to the existing binance heartbeat format
- When no orders occurred in the window, latency fields show `avg_latency=0ms max_latency=0ms orders=0`
- Stats reset each heartbeat interval — they represent the most recent window only

### Task 8: Support updown_trades.json as replay input format
**Priority:** medium
**Depends on:** 4
**Files likely affected:** updown/replay.py
**Agent:** @Backend Architect
**Allowed tools:** Read, Edit
**Success criteria:**
- `ReplayEngine.load()` also accepts the existing `updown_trades.json` format (JSON array of trade records)
- When loading from trades format, the engine extracts: `market_price` as the YES price, `timestamp_utc` converted to epoch ms, and reconstructs minimal tick data for replay
- A clear warning is logged that trade-file replay has lower fidelity than tick-log replay (no open_price, no NO price, no price_age_ms) — some signal computations will be skipped or approximate
- This allows immediate backtesting against the existing historical trade data before tick logging is deployed

## Constraints & Decisions
- The Software Architect is handling the `_process_tick` refactor into pure functions. Tasks 2-4 in this plan depend on that refactored interface. The `TickContext` dataclass (Task 2) is assigned to the Software Architect to ensure it aligns with their refactoring. Tasks 3, 4, 6, 7 are for the Backend Architect and consume the pure functions as a black box.
- Exchange timestamps from Binance (`data["T"]`) are millisecond-precision epoch integers. Polymarket WS events do not carry server timestamps consistently, so `time.time() * 1000` remains the fallback there. This means Polymarket price age calculations have wall-clock precision only.
- The replay harness runs synchronously by design. The live system is async, but backtesting has no I/O to await. This avoids the complexity of mocking asyncio infrastructure.
- JSONL format for tick logs is chosen over JSON arrays because it supports append-only writes without reading the whole file, and enables streaming reads for large datasets.
- The `updown_trades.json` replay path (Task 8) is deliberately lower fidelity. It exists to provide immediate value from existing data. Once tick logging is deployed, the JSONL path becomes the primary replay source.

## Open Decisions
1. **Tick log rotation policy**: Should `updown_ticks.jsonl` rotate by size, by date, or grow unbounded? At ~100 bytes/tick and ~10 ticks/second, that is ~86 MB/day. The user should decide on a rotation strategy before production deployment.
2. **Polymarket WS timestamp sourcing**: Polymarket CLOB WS events sometimes include a `timestamp` field but it is not documented as stable. Should we attempt to parse it when present, or consistently use wall-clock time for all Polymarket-originated timestamps?
3. **Replay fidelity for window open price**: In live mode, `open_price` comes from the BinanceWS rolling window. For replay, should we (a) require the tick log to store `open_price` per tick (chosen approach in Task 3), (b) reconstruct it from a replay-side rolling window over past ticks, or (c) both? Option (b) would be more accurate for long replays but adds complexity.
4. **Backtest output format**: Should the per-tick decision log include only trade events (entries/exits) or every tick evaluation including holds? Full tick output is useful for signal analysis but can be very large.

## Notes
- The current `TrackedMarket.discovered_at` field defaults to `time.time()` at dataclass construction time (line 262 of loop.py). This is a side effect in a dataclass default factory, which is an anti-pattern for testability. Task 1 should change this to accept an explicit timestamp parameter.
- `entry_time` is set via `time.time()` on line 952 of loop.py, immediately after `place_order` returns. This should use the tick's exchange timestamp instead, since the wall-clock time includes order execution latency and does not reflect when the signal was actually generated.
- The `MarketSnapshot.timestamp_ms` field already exists in types.py and carries `tick.timestamp_ms` in the entry path but `int(time.time() * 1000)` in the exit path (executor.py line 187). Task 1 should unify these.
- `compute_signal()` in signal.py reads `config.UPDOWN_SCALE_FACTOR` and `config.UPDOWN_MIN_BTC_PCT_CHANGE` at module load time as module-level constants. For replay, these must be configurable per-run. The replay harness should either monkey-patch these or the signal function should accept them as parameters. This is a design question for the Software Architect's refactoring.
