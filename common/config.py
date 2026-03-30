import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root is two levels up from common/config.py
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")

# Polymarket CLI binary name/path
POLYMARKET_CLI_BIN: str = os.environ.get("POLYMARKET_CLI_BIN", "polymarket")

# Project-specific data directories
ESTIMATOR_DATA_DIR: Path = REPO_ROOT / "estimator" / "data"
UPDOWN_DATA_DIR: Path = REPO_ROOT / "updown" / "data"

# LLM provider: "ollama" or "none"
# Use "none" when running inside Claude Code — the script skips all LLM calls
# and outputs raw data for Claude to analyse directly.
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "ollama")  # "none" disables all Ollama calls and uses keyword heuristics

# Ollama configuration
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Scanner thresholds
SCANNER_MIN_SCORE: float = float(os.environ.get("SCANNER_MIN_SCORE", "0.05"))
SCANNER_MARKET_LIMIT: int = int(os.environ.get("SCANNER_MARKET_LIMIT", "100"))

# Hardcoded constant — not overridable via env var
SPREAD_FETCH_LIMIT: int = 50

# MiroFish API
MIROFISH_BASE_URL: str = os.environ.get("MIROFISH_BASE_URL", "http://localhost:5001")
MIROFISH_POLL_INTERVAL_S: float = float(os.environ.get("MIROFISH_POLL_INTERVAL_S", "2.0"))
MIROFISH_POLL_TIMEOUT_S: float = float(os.environ.get("MIROFISH_POLL_TIMEOUT_S", "1200.0"))  # CPU-only MiroFish builds take 5-20 min

# Neo4j connection
NEO4J_URI: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER: str = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = os.environ.get("NEO4J_PASSWORD", "mirofish")

# Filesystem paths for research artefacts
FETCHED_DOCS_DIR: Path = Path(os.environ.get("FETCHED_DOCS_DIR", str(REPO_ROOT / "estimator" / "fetched_docs")))
RESULTS_DIR: Path = Path(os.environ.get("RESULTS_DIR", str(ESTIMATOR_DATA_DIR / "results")))

# External API keys (empty string = key absent / feature disabled)
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")

# Maximum number of Tavily news articles to fetch per topic.
NEWS_MAX_RESULTS: int = int(os.environ.get("NEWS_MAX_RESULTS", "20"))

# Research scoring
RESEARCH_MIN_EDGE: float = float(os.environ.get("RESEARCH_MIN_EDGE", "0.05"))

# Execution and monitoring settings

# DRY_MODE — when True the pipeline logs trades but never submits orders.
# WARNING: live trading requires DRY_MODE=False set explicitly in the environment.
# A missing env var ALWAYS defaults to dry mode to prevent accidental order submission.
DRY_MODE: bool = os.environ.get("DRY_MODE", "true").lower() != "false"

# RISK_PROFILE — governs exit-threshold selection in portfolio_monitor.py.
# Accepted values: "conservative", "moderate", "aggressive". Default: "conservative".
# Exit threshold numerics live exclusively in monitor/portfolio_monitor.py, not here.
RISK_PROFILE: str = os.environ.get("RISK_PROFILE", "conservative")

# Minimum abs(edge) * confidence composite score for a candidate to enter pending_trades.json.
MIN_COMPOSITE_SCORE: float = float(os.environ.get("MIN_COMPOSITE_SCORE", "0.03"))

# Default USDC stake per trade.
# WARNING: live trading requires DRY_MODE=False set explicitly in the environment.
TRADE_AMOUNT_USDC: float = float(os.environ.get("TRADE_AMOUNT_USDC", "10.0"))

# Seconds to sleep between sequential price fetches inside the portfolio monitor.
MONITOR_PRICE_FETCH_DELAY_S: float = float(os.environ.get("MONITOR_PRICE_FETCH_DELAY_S", "0.5"))

# Shared-state files for the execution and monitoring layer.
PENDING_TRADES_FILE: Path = ESTIMATOR_DATA_DIR / "pending_trades.json"
DRY_TRADES_FILE: Path = ESTIMATOR_DATA_DIR / "dry_trades.json"
MONITOR_REPORT_FILE: Path = ESTIMATOR_DATA_DIR / "monitor_report.json"
PNL_REPORT_FILE: Path = Path(os.environ.get("PNL_REPORT_FILE", str(ESTIMATOR_DATA_DIR / "pnl_report.json")))

# Gamma API — Polymarket's market-metadata service used for P&L resolution data.
GAMMA_API_BASE_URL: str = os.environ.get("GAMMA_API_BASE_URL", "https://gamma-api.polymarket.com")

# Fee rate applied when computing net P&L (fraction, e.g. 0.02 = 2%).
PNL_FEE_RATE: float = float(os.environ.get("PNL_FEE_RATE", "0.02"))

# ---------------------------------------------------------------------------
# Updown strategy — real-time BTC price → Polymarket up/down market trading
# ---------------------------------------------------------------------------

# UPDOWN_DRY_MODE — when True the updown strategy logs trades but never submits.
# Falls back to the global DRY_MODE when the env var is absent.
UPDOWN_DRY_MODE: bool = os.environ.get(
    "UPDOWN_DRY_MODE", os.environ.get("DRY_MODE", "true")
).lower() != "false"

# Minimum abs(edge) to act on an updown opportunity.
UPDOWN_EDGE_THRESHOLD: float = float(os.environ.get("UPDOWN_EDGE_THRESHOLD", "0.05"))

# Default USDC stake per updown trade.
UPDOWN_TRADE_AMOUNT_USDC: float = float(os.environ.get("UPDOWN_TRADE_AMOUNT_USDC", "5.0"))

# Binance WebSocket stream for real-time BTC/USDT trades.
BINANCE_WS_URL: str = os.environ.get(
    "BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade"
)

# Polymarket CLOB endpoints.
POLYMARKET_CLOB_WS_URL: str = os.environ.get(
    "POLYMARKET_CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)
POLYMARKET_CLOB_REST_URL: str = os.environ.get(
    "POLYMARKET_CLOB_REST_URL", "https://clob.polymarket.com"
)

# Polymarket API credentials — required for live mode (UPDOWN_DRY_MODE=false).
# Leave empty in dev; set all three in the environment for live trading.
POLYMARKET_API_KEY: str = os.environ.get("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET: str = os.environ.get("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE: str = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

# Rolling observation window for BTC price movement (seconds).
UPDOWN_WINDOW_SECONDS: int = int(os.environ.get("UPDOWN_WINDOW_SECONDS", "300"))

# Seconds between heartbeat log lines in the updown event loop.
UPDOWN_HEARTBEAT_INTERVAL_S: int = int(os.environ.get("UPDOWN_HEARTBEAT_INTERVAL_S", "10"))

# Exponential-backoff reconnect parameters for WebSocket connections.
UPDOWN_RECONNECT_BASE_DELAY_S: float = float(os.environ.get("UPDOWN_RECONNECT_BASE_DELAY_S", "1.0"))
UPDOWN_RECONNECT_MAX_DELAY_S: float = float(os.environ.get("UPDOWN_RECONNECT_MAX_DELAY_S", "60.0"))

# Scale factor: maps a BTC percentage move to a probability shift inside the
# signal engine.  A pct_change equal to UPDOWN_SCALE_FACTOR drives the implied
# probability from 0.5 to the clamp boundary (maximum conviction).
# Default 0.01 means a 0.01% BTC move shifts implied_prob by 1 unit; with the
# default UPDOWN_EDGE_THRESHOLD of 0.05, a ~0.03% move is needed to trigger a
# trade (0.0003 / 0.01 ≈ 0.03 shift → edge ≈ 0.05 after subtracting 0.5
# market price).
UPDOWN_SCALE_FACTOR: float = float(os.environ.get("UPDOWN_SCALE_FACTOR", "0.01"))

# Minimum absolute BTC percentage change (as a decimal fraction) required
# before the signal engine will consider a tick actionable.  Ticks with
# abs(pct_change) below this value are logged at DEBUG level and skipped.
# Default 0.0001 = 0.01%.
UPDOWN_MIN_BTC_PCT_CHANGE: float = float(os.environ.get("UPDOWN_MIN_BTC_PCT_CHANGE", "0.0001"))

# Maximum acceptable slippage between signal-time price and execution-time
# price, expressed as an absolute delta on the [0, 1] probability scale.
# Orders that exceed this tolerance are rejected before submission.
UPDOWN_SLIPPAGE_TOLERANCE: float = float(os.environ.get("UPDOWN_SLIPPAGE_TOLERANCE", "0.01"))

# Seconds before a market expires to proactively seed the next-window market.
# Both markets coexist during this handoff window; the expiring market remains
# tradeable until its actual TTL reaches 0.
UPDOWN_ROTATION_LEAD_TIME_S: float = float(os.environ.get("UPDOWN_ROTATION_LEAD_TIME_S", "10"))

# Tick-level JSONL logging for replay and debugging.
# Default false — zero overhead when disabled (single bool check per tick).
UPDOWN_TICK_LOG_ENABLED: bool = os.environ.get("UPDOWN_TICK_LOG_ENABLED", "false").lower() == "true"

# Tick-capture-only mode: record ticks but skip all trading logic.
UPDOWN_TICK_ONLY: bool = os.environ.get("UPDOWN_TICK_ONLY", "false").lower() == "true"

# Persistent trade log for the updown strategy.
UPDOWN_TRADES_FILE: Path = UPDOWN_DATA_DIR / "updown_trades.json"
