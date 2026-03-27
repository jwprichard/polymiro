import os
from pathlib import Path

# Polymarket CLI binary name/path
POLYMARKET_CLI_BIN: str = os.environ.get("POLYMARKET_CLI_BIN", "polymarket")

# Shared data directory — always an absolute Path
DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))

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
MIROFISH_POLL_TIMEOUT_S: float = float(os.environ.get("MIROFISH_POLL_TIMEOUT_S", "120.0"))  # increase for runs with many fetchers

# Neo4j connection
NEO4J_URI: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER: str = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = os.environ.get("NEO4J_PASSWORD", "mirofish")

# Filesystem paths for research artefacts
FETCHED_DOCS_DIR: Path = Path(os.environ.get("FETCHED_DOCS_DIR", str(Path(__file__).parent / "fetched_docs")))
RESULTS_DIR: Path = Path(os.environ.get("RESULTS_DIR", str(Path(__file__).parent / "data" / "results")))

# External API keys (empty string = key absent / feature disabled)
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")

# Research scoring
RESEARCH_MIN_EDGE: float = float(os.environ.get("RESEARCH_MIN_EDGE", "0.05"))
