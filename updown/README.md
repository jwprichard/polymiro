# updown/

Real-time BTC momentum trading on Polymarket binary-option markets. Streams BTC/USDT prices from Binance, computes directional signals, and trades YES/NO tokens on Polymarket's "Will BTC go up?" 5-minute window markets.

## How It Works

1. **Binance WS** streams BTC/USDT trades into an async queue
2. **Polymarket WS** maintains live best-bid/best-ask for tracked YES/NO tokens
3. **Signal engine** computes a momentum-derived implied probability from the BTC price move over a rolling window, then compares it to the Polymarket market price to find edge
4. **Decision functions** (pure, stateless) evaluate expiry, exit rules, and entry signals
5. **Executor** places limit orders via the Polymarket CLOB API (or logs them in dry mode)
6. **Market rotation** automatically discovers and subscribes to the next 5-minute window market before the current one expires

## Module Map

| File | Purpose |
|---|---|
| `types.py` | All data types: `PriceUpdate`, `MarketSnapshot`, `SignalResult`, `TradeIntent`, `OrderResult`, `MarketState` enum, `TickContext`, `TickDecision`, transition logic, `get_exchange_now_ms()` |
| `loop.py` | Main orchestrator: launches WS clients, runs the tick loop, manages `TrackedMarket` lifecycle, heartbeat logging, market rotation |
| `signal.py` | Pure momentum signal computation: BTC % change -> implied probability -> edge vs. market price |
| `decisions.py` | Pure decision functions extracted from the tick loop: `evaluate_expiry()`, `evaluate_exit()`, `evaluate_entry()` |
| `exit_rules.py` | Pure exit condition evaluator: stop-loss, take-profit, time-based exit |
| `strategy_config.py` | Typed YAML loader for `strategy.yml` (exit rules, thresholds) |
| `executor.py` | Order placement (live via `py-clob-client` or dry-mode logging), slippage guards, trade persistence, latency tracking |
| `binance_ws.py` | Async Binance BTC/USDT trade stream with rolling price window and auto-reconnect |
| `polymarket_ws.py` | Async Polymarket CLOB WS client with order-book tracking, REST seeding, and auto-reconnect |
| `tick_log.py` | JSONL tick logger for recording replayable tick streams with daily file rotation |
| `replay.py` | Synchronous backtest harness: replays tick logs through the pure decision functions |
| `retry.py` | Async retry helper with exponential backoff and jitter for REST calls |

## State Machine

Positions follow an explicit `MarketState` lifecycle. Invalid transitions raise `InvalidTransitionError`.

```
IDLE ──> ENTERING ──> ENTERED ──> EXITING ──> COOLDOWN ──> IDLE
           │                        │
           └──> IDLE (on failure)   └──> IDLE (no reentry cooldown)

ENTERED ──> IDLE (on market expiry)
```

- **IDLE** — no position, eligible for entry signals
- **ENTERING** — order submitted, awaiting fill (guards against duplicate orders)
- **ENTERED** — position open, exit rules evaluated each tick
- **EXITING** — exit order submitted, awaiting fill
- **COOLDOWN** — post-exit pause using an absolute `cooldown_until` timestamp

## Running

### Live trading

```bash
UPDOWN_DRY_MODE=false \
POLYMARKET_API_KEY=... \
POLYMARKET_API_SECRET=... \
POLYMARKET_API_PASSPHRASE=... \
python main.py updown
```

### Dry-run mode (default)

```bash
python main.py updown --dry-run
```

Or equivalently:

```bash
UPDOWN_DRY_MODE=true python -m updown
```

Logs all signal evaluations, trade intents, and exit decisions without placing real orders.

### Record ticks for backtesting

```bash
UPDOWN_TICK_LOG_ENABLED=true python main.py updown --dry-run
```

Writes JSONL files to `data/updown_ticks_YYYY-MM-DD.jsonl`. Each line is a serialized `TickContext` snapshot with everything needed to replay through the decision pipeline.

### Backtest

```bash
# Replay recorded tick logs
python main.py backtest --file data/updown_ticks_2026-03-30.jsonl

# With custom parameters
python main.py backtest \
  --file data/updown_ticks_2026-03-30.jsonl \
  --edge-threshold 0.03 \
  --strategy strategy.yml \
  --output results.json

# Replay old trade files (lower fidelity — no open_price, no NO price)
python main.py backtest --file data/updown_trades.json
```

Output is a JSON summary: total ticks, signals generated, trades entered/exited, win/loss count, total P&L, and max drawdown.

## Configuration

All settings are environment variables with safe defaults. Key ones:

| Variable | Default | Description |
|---|---|---|
| `UPDOWN_DRY_MODE` | `true` | Log trades without submitting orders |
| `UPDOWN_EDGE_THRESHOLD` | `0.05` | Minimum absolute edge to trigger a trade |
| `UPDOWN_TRADE_AMOUNT_USDC` | `5.0` | Position size per trade |
| `UPDOWN_WINDOW_SECONDS` | `300` | Rolling BTC observation window (seconds) |
| `UPDOWN_SCALE_FACTOR` | `0.01` | Maps BTC % move to probability shift |
| `UPDOWN_MIN_BTC_PCT_CHANGE` | `0.0001` | Noise gate: minimum BTC move to consider |
| `UPDOWN_SLIPPAGE_TOLERANCE` | `0.01` | Max price drift between signal and execution |
| `UPDOWN_TICK_LOG_ENABLED` | `false` | Enable JSONL tick recording for replay |
| `UPDOWN_HEARTBEAT_INTERVAL_S` | `10` | Seconds between heartbeat log lines |
| `UPDOWN_ROTATION_LEAD_TIME_S` | `10` | Seconds before expiry to seed next market |
| `UPDOWN_RECONNECT_BASE_DELAY_S` | `1.0` | WS reconnect initial backoff |
| `UPDOWN_RECONNECT_MAX_DELAY_S` | `60.0` | WS reconnect max backoff |

## Exit Rules

Configured in `strategy.yml`:

```yaml
exit_rules:
  take_profit:
    enabled: true
    target_delta: 0.08    # close when position price rises by 0.08
  stop_loss:
    enabled: true
    max_loss_delta: 0.05  # close when position price drops by 0.05
  time_exit:
    enabled: true
    max_hold_seconds: 240 # close after 4 minutes regardless
  allow_reentry: true     # skip COOLDOWN and return to IDLE after exit
```

Evaluation order: stop-loss -> take-profit -> time-exit (first match wins).

## Latency Tracking

Every order logs `[LATENCY] tick_to_order=Xms` — the time from the Binance tick that generated the signal to the moment the order is submitted. Heartbeat logs include rolling `avg_latency`, `max_latency`, and order count, resetting each interval.

## Time Sources

- **Per-tick decisions**: Binance `tick.timestamp_ms` via `get_exchange_now_ms()` — single canonical source
- **Polymarket price age**: wall-clock `time.time()` (Polymarket WS timestamps are undocumented)
- **Heartbeat, logging, TTL display**: wall-clock `time.time()`

## Dependencies

Uses only stdlib plus existing project dependencies:

- `websockets` — Binance and Polymarket WS connections
- `aiohttp` — Polymarket REST seeding and HTTP calls
- `py-clob-client` — Polymarket CLOB order submission
- `pyyaml` — strategy.yml parsing
