# PROJECT MAP
_Scanned: 2026-03-30T12:00:00Z_
_Commit: 261a425_

## Stack
- **Language:** Python
- **Runtime:** Python 3.14 (inferred from `config.cpython-314.pyc`)
- **Framework:** none detected (plain Python modules; Flask lives inside the external MiroFish service only)
- **Database:** Neo4j 5.15 (Bolt on localhost:7687, containerised via docker-compose)
- **LLM:** Ollama (qwen2.5:1.5b) local; configurable via `OLLAMA_MODEL` env var
- **Key libraries:** requests, beautifulsoup4, tavily-python, ollama, neo4j, aiohttp, websockets, python-dotenv, pyyaml
- **External tools:** polymarket-cli (Rust binary, subprocess calls), py-clob-client (Polymarket CLOB SDK, used in updown executor for live order placement)
- **Test runner:** pytest (used in `tests/`)

## Structure
- `scanner/` -- Polymarket scanning: `polymarket_client.py` (CLI subprocess wrapper), `models.py` (Market/Opportunity dataclasses), `opportunity_scorer.py` (weighted scoring formula), `scanner_agent.py` (scan orchestrator, writes `data/opportunities.json`)
- `fetchers/` -- Data connectors: `base_fetcher.py` (ABC), `news_fetcher.py` (Tavily), `weather_fetcher.py` (Open-Meteo), `wiki_fetcher.py` (Wikipedia REST), `web_fetcher.py` (requests + BS4). Output to `fetched_docs/{run_id}/`
- `mirofish/` -- MiroFish HTTP bridge (`bridge.py`: ontology generate + graph build + poll) and Neo4j query + LLM probability layer (`neo4j_query.py`)
- `research/` -- Research orchestration: `research_agent.py` (full pipeline loop), `query_interpreter.py` (LLM/heuristic -> FetchPlan), `source_router.py` (source label -> fetcher class name), `_llm_utils.py` (Ollama JSON call with retry)
- `selector/` -- Opportunity ranking: `opportunity_selector.py` (scores results by edge * confidence, writes `data/pending_trades.json`). Note: `main.py` imports `selector.selector_agent` which resolves via `selector/__init__.py` re-export
- `trading/` -- Trade execution: `trade_executor.py` (interactive review + dry/live order submission via polymarket-cli)
- `monitor/` -- Portfolio monitoring: `portfolio_monitor.py` (reads open positions, fetches current prices, emits HOLD/EXIT per risk profile, writes `data/monitor_report.json`)
- `pnl/` -- P&L calculation and resolution tracking: `tracker.py` (loads dry-mode trades from `dry_trades.json` + `updown_trades.json`, deduplicates by trade_id, batches Gamma API calls by condition_id, computes P&L for resolved markets, writes `pnl_report.json`), `calculator.py` (shares/payout/gross/net P&L with fee deduction), `gamma_client.py` (Gamma API resolution checker)
- `updown/` -- Real-time BTC 5-min up/down strategy: `loop.py` (async orchestrator coordinating Binance + Polymarket streams with auto-rotation to next 5-min window, queue backpressure with stale tick dropping, early market rotation before expiry), `polymarket_ws.py` (CLOB websocket client with REST book seeding), `binance_ws.py` (BTC/USDT trade stream), `signal.py` (momentum signal engine), `executor.py` (order placement via py-clob-client + exit intent builder + slippage guard), `exit_rules.py` (pure TP/SL/time exit evaluator), `strategy_config.py` (typed YAML config loader), `types.py` (shared dataclasses incl. signal_price on TradeIntent), `retry.py` (reusable async retry with exponential backoff and jitter)
- `utils/` -- Shared utilities: `io.py` (atomic JSON write, flock-guarded append)
- `data/` -- Shared state: `opportunities.json`, `research_queue.json`, `pending_trades.json`, `dry_trades.json`, `updown_trades.json`, `monitor_report.json`, `pnl_report.json`, `results/{market_id}.json`
- `fetched_docs/` -- Per-run raw document dumps keyed by market condition_id (hex)
- `scripts/` -- Smoke tests (`smoke_test.py`, `smoke_test_llm.py`, `smoke_test_research.py`) and `install_mirofish.sh`
- `tests/` -- Pytest test suite: `test_polymarket_ws.py` (REST bootstrap seeding tests for updown WS client)
- `MiroFish-Offline/` -- Git submodule / local clone of the MiroFish service (Flask backend + Vue frontend); built by docker-compose
- `.pace/` -- PACE agent artefacts (PROJECT.md, PLAN.md, STATE.md, agent registry)

## Entry Points
- `main.py` -- CLI entry point with subcommands: `scan`, `research`, `select`, `review [--dry-run]`, `monitor [--profile]`, `updown [--dry-run] [--edge-threshold] [--strategy]`, `pnl [--reset]`. Version 0.1.0.
- `scanner/scanner_agent.py` -- `run_scan()` / `__main__`: one full Polymarket scan cycle
- `research/research_agent.py` -- `run_research()` / `__main__`: full fetch -> graph -> estimate pipeline for the top unprocessed opportunity
- `selector/opportunity_selector.py` -- `run_selector()` / `__main__`: scores and ranks research results into `pending_trades.json`
- `trading/trade_executor.py` -- `present_for_review()` + `execute_trade()`: interactive trade approval and execution
- `monitor/portfolio_monitor.py` -- `run_monitor()` / `main()` / `__main__`: scans open positions and writes `monitor_report.json`
- `mirofish/bridge.py` -- `build_graph(question, doc_paths)`: convenience wrapper with automatic retry (max 2 attempts)
- `updown/loop.py` -- `run(strategy_config)`: async orchestrator that seeds markets from Gamma REST API, bootstraps order-book prices from CLOB REST, then runs Binance + Polymarket WS streams with signal-driven trade execution and exit rule evaluation
- `pnl/tracker.py` -- `run()` / `__main__`: settles dry-mode trades against Gamma API resolution data, writes `data/pnl_report.json`

## Key Config
- `config.py` -- All runtime constants: paths (`DATA_DIR`, `FETCHED_DOCS_DIR`, `RESULTS_DIR`), API keys (`TAVILY_API_KEY`, `POLYMARKET_API_KEY/SECRET/PASSPHRASE`), thresholds (`SCANNER_MIN_SCORE`, `RESEARCH_MIN_EDGE`, `MIN_COMPOSITE_SCORE`, `UPDOWN_EDGE_THRESHOLD`, `UPDOWN_SCALE_FACTOR`, `UPDOWN_MIN_BTC_PCT_CHANGE`, `UPDOWN_SLIPPAGE_TOLERANCE`), service URLs (`MIROFISH_BASE_URL`, `NEO4J_URI`, `OLLAMA_HOST`, `BINANCE_WS_URL`, `POLYMARKET_CLOB_WS_URL`, `POLYMARKET_CLOB_REST_URL`, `GAMMA_API_BASE_URL`), execution settings (`DRY_MODE`, `UPDOWN_DRY_MODE`, `RISK_PROFILE`, `TRADE_AMOUNT_USDC`, `UPDOWN_TRADE_AMOUNT_USDC`), timing (`UPDOWN_ROTATION_LEAD_TIME_S`), P&L settings (`PNL_FEE_RATE`, default 0.02; `PNL_REPORT_FILE`). All overridable via env vars; loaded from `.env` via python-dotenv.
- `strategy.yml` -- Updown strategy configuration: `exit_rules` (take_profit target_delta 0.06, stop_loss max_loss_delta 0.04, time_exit max_hold_seconds 240, allow_reentry false), `execution` (slippage_tolerance 0.01), plus skeleton sections for signal and features.
- `docker-compose.yml` -- Four services: `ollama` (ROCm image), `neo4j` (5.15 with healthcheck), `mirofish` (built from `./MiroFish-Offline`), `ollama-pull` (bootstrap model pull). Requires `OLLAMA_MODEL` in `.env`.
- `.env` -- Sets `OLLAMA_MODEL`, `TAVILY_API_KEY`, Polymarket API credentials, and trade mode flags; not checked in
- `.gitignore` -- Excludes `__pycache__`, `.env`, `data/*.json`, `MiroFish-Offline/`

## Conventions
- snake_case for modules and variables; PascalCase for classes
- Feature-by-type folder organisation: each top-level package owns one functional layer of the pipeline
- Atomic JSON writes throughout: write to `.tmp.json` sibling, then `os.replace()` to final path
- Concurrent write safety via `fcntl.flock` on sidecar `.lock` files (POSIX only; degrades gracefully)
- All external-service errors wrapped in module-specific exception classes (`PolymarketClientError`, `MiroFishError`, `Neo4jQueryError`, `FetcherError`, `SelectorError`, `TradeExecutionError`, `MonitorError`) so callers never see third-party exceptions
- `LLM_PROVIDER = "none"` disables all Ollama calls and falls back to keyword heuristics -- used when running inside Claude Code
- `DRY_MODE` defaults to True; live trading requires explicit `DRY_MODE=false` in the environment
- MiroFish bridge uses retry-before-propagate: one automatic retry with fresh project_id and 5 s backoff
- Research pipeline communicates via JSON files in `data/`: `opportunities.json` -> `research_queue.json` -> `results/` -> `pending_trades.json` -> `dry_trades.json` -> `monitor_report.json` -> `pnl_report.json`
- CLI dispatches subcommands via `argparse` with lazy imports to keep startup fast
- Updown strategy seeds order-book prices synchronously from CLOB REST API before opening the WebSocket
- Exit rules are pure functions (no I/O, no side effects) evaluated on every tick for open positions
- Slippage protection is a pure function (`check_slippage`) with no side effects; exit trades get 2x tolerance
- Queue backpressure: tick processor drains stale ticks when queue depth exceeds threshold, keeping only the latest
- REST market-seeding calls use `retry_async()` (exponential backoff, base 2s, max 15s, jitter) from `updown/retry.py`
- Early market rotation: proactively seeds the next-window market when current market TTL drops below `UPDOWN_ROTATION_LEAD_TIME_S`
- Strategy parameters (exit thresholds, toggles) live in `strategy.yml`, not in `config.py`

## Test Setup
- **Runner:** pytest
- **Location:** `tests/` (formal suite), `scripts/` (ad-hoc smoke tests)
- **Command:** `pytest tests/` (unit tests), `python scripts/smoke_test.py` (scanner smoke), `python scripts/smoke_test_research.py` (research pipeline smoke)
