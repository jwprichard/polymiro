# Polymarket × MiroFish

Autonomous pipeline that scans Polymarket for mispriced markets, fetches real-world data via live search, builds a knowledge graph via MiroFish, then queries it with a local LLM to estimate true probabilities.

```
Polymarket scan → opportunities.json
  → QueryInterpreter (LLM) → FetchPlan
  → Fetchers (Tavily news, Wikipedia, weather, web)
  → MiroFish → Neo4j knowledge graph
  → LLM probability estimate
  → data/results/{market_id}.json
```

---

## Prerequisites

### 1. Python 3.12+

```bash
pip install requests beautifulsoup4 tavily-python ollama neo4j
```

### 2. Rust + polymarket CLI

```bash
curl https://sh.rustup.rs -sSf | sh -s -- -y
source "$HOME/.cargo/env"
cargo install --git https://github.com/Polymarket/polymarket-cli
```

### 3. Ollama + model

```bash
# Install Ollama: https://ollama.com/download
ollama pull qwen2.5:1.5b
```

### 4. Neo4j 5.15

```bash
docker run -d \
  --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/mirofish \
  neo4j:5.15
```

### 5. MiroFish (local knowledge graph API)

```bash
git clone https://github.com/nikmcfly/MiroFish-Offline /tmp/MiroFish-Offline
cd /tmp/MiroFish-Offline
pip install -r backend/requirements.txt
FLASK_PORT=5001 python3 backend/run.py &
```

> MiroFish requires Ollama to be running — it uses it internally for ontology generation.
> On CPU, graph builds take several minutes. The pipeline continues gracefully if MiroFish times out.

---

## Configuration

Create a `.env` file in the project root (never committed):

```bash
POLYMARKET_CLI_BIN=/path/to/polymarket
TAVILY_API_KEY=tvly-xxxxxxxxxxxx
```

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_CLI_BIN` | `polymarket` | Path to the polymarket CLI binary |
| `TAVILY_API_KEY` | _(required for news search)_ | Get a free key at [tavily.com](https://tavily.com) |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | Ollama model (1.5b recommended for CPU) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API URL |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `NEO4J_PASSWORD` | `mirofish` | Neo4j password |
| `MIROFISH_BASE_URL` | `http://localhost:5001` | MiroFish API URL |
| `LLM_PROVIDER` | `ollama` | Set to `none` to skip all LLM calls (testing) |

---

## Running

**Step 1 — Scan for opportunities**

```bash
env $(cat .env | xargs) python3 -m scanner.scanner_agent
```

Writes ranked opportunities to `data/opportunities.json`.

**Step 2 — Run prediction on top opportunity**

```bash
env $(cat .env | xargs) python3 -m research.research_agent
```

Fetches live data, builds knowledge graph, estimates probability. Result written to `data/results/{market_id}.json`.

Run Step 2 repeatedly to process the next opportunity in the queue each time.

**Result format:**

```json
{
  "market_id": "0xabc...",
  "question": "Will Italy qualify for the 2026 FIFA World Cup?",
  "predicted_probability": 0.55,
  "edge": -0.20,
  "evidence_summary": "Italy is in the UEFA play-offs...",
  "graph_id": "abc-123",
  "scanned_at": "2026-03-27T03:05:49Z"
}
```

`edge` = our estimate minus Polymarket price. Positive = market underpricing YES, negative = overpricing YES.

---

## Smoke tests

```bash
# End-to-end pipeline (no LLM calls)
LLM_PROVIDER=none python3 scripts/smoke_test_research.py

# LLM component test (no Ollama calls)
LLM_PROVIDER=none python3 scripts/smoke_test_llm.py
```

---

## Project structure

```
config.py               — all configuration constants (env-overridable)
scanner/                — Polymarket market fetching and opportunity scoring
fetchers/               — data connectors (Tavily news, Wikipedia, weather, web)
mirofish/               — MiroFish bridge + Neo4j query + LLM probability estimator
research/               — query interpreter, source router, orchestration loop
data/                   — shared state (opportunities.json, results/)
scripts/                — smoke tests and utilities
```
