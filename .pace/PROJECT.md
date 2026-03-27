# PROJECT MAP
_Scanned: 2026-03-26T00:00:00+00:00_
_Commit: bede729_

## Stack
- **Language:** Python 3.12
- **Runtime:** Python 3.12 (CPython)
- **Framework:** none detected
- **Database:** Neo4j 5.15 (local, graph store via MiroFish)
- **Test runner:** none detected (manual smoke test script only)

## Structure
- `scanner/` — Polymarket market fetching, opportunity scoring, and scan orchestration
- `scripts/` — developer utilities; currently holds the end-to-end smoke test
- `data/` — shared state directory; holds `opportunities.json` and future research/results files
- `config.py` — top-level configuration constants and env-var overrides

## Entry Points
- `scanner/scanner_agent.py` — runnable directly (`python scanner/scanner_agent.py`); executes one full scan cycle and writes `data/opportunities.json`
- `scripts/smoke_test.py` — end-to-end pipeline smoke test; validates scan output schema and atomic-write guarantee

## Key Config
- `config.py` — API keys placeholders, service URLs (Ollama, polymarket-cli), scanner thresholds (`SCANNER_MIN_SCORE=0.05`, `SCANNER_MARKET_LIMIT=100`, `SPREAD_FETCH_LIMIT=50`), and `LLM_PROVIDER` toggle (`ollama` | `none`)
- `.gitignore` — excludes `data/*.json`, `__pycache__/`, `.env`, compiled Python files

## Conventions
- Dataclasses used for all domain models (`Market`, `Opportunity` in `scanner/models.py`)
- `PolymarketClientError` wraps all subprocess and JSON errors — callers never see raw `subprocess.CalledProcessError`
- Atomic file writes via `.tmp.json` + `os.replace()` pattern
- `LLM_PROVIDER="none"` mode uses keyword heuristics to skip all Ollama calls (safe for Claude Code runs)
- Config values overridable via environment variables; constants that are not overridable are marked with inline comment
- Submodules expose `__init__.py` for clean imports; all public logic lives in named modules, not `__init__`
- Broad exception catch in LLM paths is intentional and commented (`# noqa: BLE001`)

## Test Setup
- **Runner:** none (no pytest or unittest suite yet)
- **Location:** `scripts/smoke_test.py`
- **Command:** `python scripts/smoke_test.py`
