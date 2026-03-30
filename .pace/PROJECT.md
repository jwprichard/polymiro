# PROJECT MAP
_Scanned: 2026-03-30T08:00:00Z_
_Commit: 82fd2ad_

## Stack
- **Language:** Python
- **Runtime:** Python 3.14 (inferred from `config.cpython-314.pyc`)
- **Framework:** none detected (plain Python modules; Flask lives inside the external MiroFish service only)
- **Database:** Neo4j 5.15 (Bolt on localhost:7687, containerised via docker-compose)
- **LLM:** Ollama (qwen2.5:1.5b) local; configurable via `OLLAMA_MODEL` env var
- **Key libraries:** requests, beautifulsoup4, tavily-python, ollama, neo4j, aiohttp, websockets, python-dotenv, pyyaml
- **External tools:** polymarket-cli (Rust binary, subprocess calls), py-clob-client (Polymarket CLOB SDK, used in updown executor for live order placement)
- **Test runner:** pytest (used in `updown/tests/` and `estimator/tests/`)

## Structure
- `common/` -- Shared utilities: `config.py` (all runtime constants, API keys, data dir paths), `io.py` (atomic JSON write, flock-guarded append), `log.py` (category-based structured logging with `ulog.<category>.<level>()` interface, runtime filter toggle via `UPDOWN_LOG_CATEGORIES` env var or CLI `--filter` flag)
- `estimator/` -- Probability estimation pipeline (top-level package)
- `estimator/scanner/` -- Polymarket scanning: `polymarket_client.py` (CLI subprocess wrapper), `models.py` (Market/Opportunity dataclasses), `opportunity_scorer.py` (weighted scoring formula), `scanner_agent.py` (scan orchestrator, writes `estimator/data/opportunities.json`)
- `estimator/fetchers/` -- Data connectors: `base_fetcher.py` (ABC), `news_fetcher.py` (Tavily), `weather_fetcher.py` (Open-Meteo), `wiki_fetcher.py` (Wikipedia REST), `web_fetcher.py` (requests + BS4). Output to `estimator/fetched_docs/{run_id}/`
- `estimator/mirofish/` -- MiroFish HTTP bridge (`bridge.py`: ontology generate + graph build + poll) and Neo4j query + LLM probability layer (`neo4j_query.py`)
- `estimator/research/` -- Research orchestration: `research_agent.py` (full pipeline loop), `query_interpreter.py` (LLM/heuristic -> FetchPlan), `source_router.py` (source label -> fetcher class name), `_llm_utils.py` (Ollama JSON call with retry)
- `estimator/selector/` -- Opportunity ranking: `opportunity_selector.py` (scores results by edge * confidence, writes `estimator/data/pending_trades.json`). Note: `main.py` imports `selector.selector_agent` which resolves via `selector/__init__.py` re-export
- `estimator/trading/` -- Trade execution: `trade_executor.py` (interactive review + dry/live order submission via polymarket-cli)
- `estimator/monitor/` -- Portfolio monitoring: `portfolio_monitor.py` (reads open positions, fetches current prices, emits HOLD/EXIT per risk profile, writes `estimator/data/monitor_report.json`)
- `estimator/scripts/` -- Smoke tests (`smoke_test.py`, `smoke_test_llm.py`, `smoke_test_research.py`) and `install_mirofish.sh`
- `estimator/tests/` -- Estimator test suite (currently empty placeholder)
- `estimator/data/` -- Estimator state: `opportunities.json`, `research_queue.json`, `pending_trades.json`, `dry_trades.json`, `monitor_report.json`, `pnl_report.json`, `results/{market_id}.json`
- `estimator/fetched_docs/` -- Per-run raw document dumps keyed by market condition_id (hex)
- `estimator/MiroFish-Offline/` -- Git submodule / local clone of the MiroFish service (Flask backend + Vue frontend); built by docker-compose
- `estimator/mirofish-docker/` -- MiroFish Docker configuration
- `updown/` -- Real-time BTC 5-min up/down strategy: `loop.py` (async orchestrator coordinating Binance + Polymarket streams with auto-rotation to next 5-min window, queue backpressure with stale tick dropping, early market rotation before expiry), `polymarket_ws.py` (CLOB websocket client with REST book seeding), `binance_ws.py` (BTC/USDT trade stream), `signal.py` (momentum signal engine), `executor.py` (order placement via py-clob-client + exit intent builder + slippage guard), `decisions.py` (pure decision state machine, no I/O), `exit_rules.py` (pure TP/SL/time exit evaluator), `strategy_config.py` (typed YAML config loader), `types.py` (shared dataclasses), `retry.py` (async retry with exponential backoff and jitter), `tick_log.py` (JSONL tick logging), `replay.py` (tick replay harness)
- `updown/strategies/` -- Strategy YAML configs: `btc_lag_arbitrage.yml` (BTC 5-min up/down lag arbitrage — sections: strategy, signals, entry, exit, risk, execution, filters, timing)
- `updown/pnl/` -- P&L calculation and resolution tracking: `tracker.py` (loads dry-mode trades, deduplicates by trade_id, batches Gamma API calls by condition_id, computes P&L for resolved markets, writes `pnl_report.json`), `calculator.py` (shares/payout/gross/net P&L with fee deduction), `gamma_client.py` (Gamma API resolution checker)
- `updown/tests/` -- Updown test suite: 16 test modules covering `binance_ws`, `decisions`, `executor`, `exit_rules`, `loop`, `pnl_calculator`, `pnl_gamma_client`, `pnl_tracker`, `polymarket_ws`, `replay`, `retry`, `signal`, `strategy_config`, `tick_log`, `types` + `conftest.py`
- `updown/data/` -- Updown state: JSONL tick logs (`updown_ticks_YYYY-MM-DD.jsonl`), `updown_trades.json`, `backtests/`
- `.pace/` -- PACE agent artefacts (PROJECT.md, PLAN.md, STATE.md, agent registry)

## Entry Points
- `main.py` -- CLI entry point with subcommands: `scan`, `research`, `select`, `review [--dry-run]`, `monitor [--profile]`, `updown [--dry-run] [--edge-threshold] [--strategy] [--no-tick-log] [--tick-only]`, `backtest --file <path> [--strategy] [--edge-threshold] [--output]`, `pnl [--reset]`. Global flags: `--log LEVEL`, `--filter CATEGORIES`. Version 0.1.0.
- `estimator/scanner/scanner_agent.py` -- `run_scan()` / `__main__`: one full Polymarket scan cycle
- `estimator/research/research_agent.py` -- `run_research()` / `__main__`: full fetch -> graph -> estimate pipeline for the top unprocessed opportunity
- `estimator/selector/opportunity_selector.py` -- `run_selector()` / `__main__`: scores and ranks research results into `pending_trades.json`
- `estimator/trading/trade_executor.py` -- `present_for_review()` + `execute_trade()`: interactive trade approval and execution
- `estimator/monitor/portfolio_monitor.py` -- `run_monitor()` / `main()` / `__main__`: scans open positions and writes `monitor_report.json`
- `estimator/mirofish/bridge.py` -- `build_graph(question, doc_paths)`: convenience wrapper with automatic retry (max 2 attempts)
- `updown/loop.py` -- `run(strategy_config)`: async orchestrator that seeds markets from Gamma REST API, bootstraps order-book prices from CLOB REST, then runs Binance + Polymarket WS streams with signal-driven trade execution and exit rule evaluation
- `updown/pnl/tracker.py` -- `run()` / `__main__`: settles dry-mode trades against Gamma API resolution data, writes `updown/data/pnl_report.json`
- `updown/replay.py` -- `ReplayEngine.load()` + `run()` + `summary()`: offline tick replay through the decision pipeline

## Key Config
- `common/config.py` -- All runtime constants: paths (`ESTIMATOR_DATA_DIR`, `UPDOWN_DATA_DIR`, `FETCHED_DOCS_DIR`, `RESULTS_DIR`), API keys (`TAVILY_API_KEY`, `POLYMARKET_API_KEY/SECRET/PASSPHRASE`), thresholds (`SCANNER_MIN_SCORE`, `RESEARCH_MIN_EDGE`, `MIN_COMPOSITE_SCORE`, `UPDOWN_EDGE_THRESHOLD`, `UPDOWN_SCALE_FACTOR`, `UPDOWN_MIN_BTC_PCT_CHANGE`, `UPDOWN_SLIPPAGE_TOLERANCE`), service URLs (`MIROFISH_BASE_URL`, `NEO4J_URI`, `OLLAMA_HOST`, `BINANCE_WS_URL`, `POLYMARKET_CLOB_WS_URL`, `POLYMARKET_CLOB_REST_URL`, `GAMMA_API_BASE_URL`), execution settings (`DRY_MODE`, `UPDOWN_DRY_MODE`, `RISK_PROFILE`, `TRADE_AMOUNT_USDC`, `UPDOWN_TRADE_AMOUNT_USDC`), timing (`UPDOWN_ROTATION_LEAD_TIME_S`), tick capture (`UPDOWN_TICK_LOG_ENABLED`, `UPDOWN_TICK_ONLY`), P&L settings (`PNL_FEE_RATE`, default 0.02; `PNL_REPORT_FILE`). All overridable via env vars; loaded from `.env` via python-dotenv.
- `updown/strategies/btc_lag_arbitrage.yml` -- Primary strategy config: `strategy` (metadata), `signals` (momentum, 300s lookback, EMA smoothing), `entry` (min_edge 0.05, min_confidence 0.6), `exit` (time_exit max_hold 240s), `risk` (position_size 5 USDC, TP delta 0.06, SL delta 0.04, no reentry), `execution` (limit orders, slippage 0.01), `filters` (btc_5min_updown, min_liquidity 50 USDC), `timing` (rotation lead 30s, cooldown 10s).
- `docker-compose.yml` -- Four services: `ollama` (ROCm image), `neo4j` (5.15 with healthcheck), `mirofish` (built from `./MiroFish-Offline`), `ollama-pull` (bootstrap model pull). Requires `OLLAMA_MODEL` in `.env`.
- `.env` -- Sets `OLLAMA_MODEL`, `TAVILY_API_KEY`, Polymarket API credentials, and trade mode flags; not checked in
- `.gitignore` -- Excludes `__pycache__`, `.env`, `data/*.json`, `MiroFish-Offline/`, `**/updown_ticks_*.jsonl`

## Conventions
- snake_case for modules and variables; PascalCase for classes
- Three top-level packages: `common/` (shared), `estimator/` (probability pipeline), `updown/` (real-time trading bot)
- Feature-by-type folder organisation: each package owns one functional layer of the pipeline
- Atomic JSON writes throughout: write to `.tmp.json` sibling, then `os.replace()` to final path
- Concurrent write safety via `fcntl.flock` on sidecar `.lock` files (POSIX only; degrades gracefully)
- All external-service errors wrapped in module-specific exception classes (`PolymarketClientError`, `MiroFishError`, `Neo4jQueryError`, `FetcherError`, `SelectorError`, `TradeExecutionError`, `MonitorError`) so callers never see third-party exceptions
- `LLM_PROVIDER = "none"` disables all Ollama calls and falls back to keyword heuristics — used when running inside Claude Code
- `DRY_MODE` defaults to True; live trading requires explicit `DRY_MODE=false` in the environment
- MiroFish bridge uses retry-before-propagate: one automatic retry with fresh project_id and 5 s backoff
- Research pipeline communicates via JSON files in `estimator/data/`: `opportunities.json` -> `research_queue.json` -> `results/` -> `pending_trades.json` -> `dry_trades.json` -> `monitor_report.json` -> `pnl_report.json`
- CLI dispatches subcommands via `argparse` with lazy imports to keep startup fast
- Updown strategy seeds order-book prices synchronously from CLOB REST API before opening the WebSocket
- Exit rules are pure functions (no I/O, no side effects) evaluated on every tick for open positions
- Slippage protection is a pure function (`check_slippage`) with no side effects; exit trades get 2x tolerance
- Queue backpressure: tick processor drains stale ticks when queue depth exceeds threshold, keeping only the latest
- REST market-seeding calls use `retry_async()` (exponential backoff, base 2s, max 15s, jitter) from `updown/retry.py`
- Early market rotation: proactively seeds the next-window market when current market TTL drops below `UPDOWN_ROTATION_LEAD_TIME_S`
- Strategy parameters (exit thresholds, toggles, entry/signal/filter/timing config) live in `updown/strategies/*.yml`, not in `common/config.py`
- Decision logic extracted into pure state machine (`updown/decisions.py`) for testability; no I/O or WS client imports allowed inside that module
- Centralized structured logging via `common/log.py`: use `ulog.<category>.<level>()` instead of raw `logger.*`; filterable per-category via `UPDOWN_LOG_CATEGORIES` env var or CLI `--filter` flag

## Test Setup
- **Runner:** pytest
- **Location:** `updown/tests/` (16 test modules, full updown coverage), `estimator/tests/` (placeholder, currently empty), `estimator/scripts/` (ad-hoc smoke tests)
- **Command:** `pytest updown/tests/` (updown unit tests), `pytest estimator/tests/` (estimator tests), `python estimator/scripts/smoke_test.py` (scanner smoke), `python estimator/scripts/smoke_test_research.py` (research pipeline smoke)
