# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket × MiroFish: an autonomous pipeline that scans Polymarket for mispriced markets, fetches real-world data, builds a knowledge graph via MiroFish, then queries that graph directly for probability estimates — **bypassing MiroFish's social simulation layer entirely**.

## Key Architectural Decision

MiroFish is used **only for knowledge graph construction** (documents → Neo4j). Its social simulation (`/api/simulation/start`) is never called. The pipeline stops after the graph is built and queries Neo4j directly with an LLM.

```
documents → MiroFish (POST /ontology/generate) → Neo4j graph → LLM probability query → PredictionResult
```

## Planned Project Structure

```
polymarket-mirofish/
├── scanner/          # Polymarket scanning + opportunity scoring
├── fetchers/         # Data connectors (weather, news, wiki, crypto, macro)
├── mirofish/         # MiroFish API bridge + Neo4j query layer
├── research/         # Research agent loop + topic→fetcher routing
├── data/             # Shared state: opportunities.json, research_queue.json, results/
├── config.py         # API keys, service URLs, intervals
└── main.py           # Entry point / orchestrator
```

## Local Services (Already Running)

- **MiroFish** — local, provides graph construction API
- **Neo4j 5.15** — local, stores the knowledge graphs built by MiroFish
- **Ollama** (qwen2.5) — local LLM used for graph queries and the query interpreter
- **polymarket-cli** — Rust binary, pre-installed, used with `--output json`

## Shared State Contract

Agents communicate via JSON files in `data/`:
- `opportunities.json` — scanner writes, research agents consume
- `research_queue.json` — tracks which markets have been researched
- `results/{market_id}.json` — per-market PredictionResult output

The `edge` field in results = our predicted probability − Polymarket current price. Positive = underpriced YES, negative = overpriced YES.

## MiroFish API Usage

```python
# Graph build (use this):
POST /ontology/generate   # multipart form: files + market question
GET  /data/{graph_id}     # poll until ready

# Never call:
POST /api/simulation/start
```

## Neo4j Query Pattern

```cypher
MATCH (e:Entity {graph_id: $graph_id})-[r]->(e2:Entity)
RETURN e.name, e.type, type(r), r.weight, e2.name, e2.type
LIMIT 200
```

## Fetcher Connectors

Each fetcher returns plain-text documents written to `./fetched_docs/{run_id}/`. Implemented as subclasses of `base_fetcher.py`.

| Connector | Source | Auth |
|---|---|---|
| WeatherFetcher | Open-Meteo | None |
| NewsFetcher | Tavily API | API key |
| WikiFetcher | Wikipedia REST | None |
| WebFetcher | requests + BS4 | None |
| CryptoFetcher | CoinGecko | None |
| MacroFetcher | FRED API | Free key |

Topic → fetcher routing lives in `research/source_router.py`.

## Build Order

Build in this sequence to validate each layer before adding the next:
1. `polymarket_client.py` + scanner shell
2. `opportunities.json` writer
3. `WeatherFetcher` + MiroFish bridge (graph only, end-to-end)
4. `neo4j_query.py` probability layer
5. `query_interpreter.py` (LLM → FetchPlan)
6. Full `research_agent.py` loop
7. Remaining fetchers
8. OpenViking integration (Phase 5, future)

## Python Dependencies

```
requests, beautifulsoup4, tavily-python, ollama, neo4j
```

## Open Design Decisions

- Scanner cadence: 30 min suggested
- Minimum edge threshold to surface: ±0.05 suggested
- Market filter scope: all markets vs. specific tags (crypto, weather, sports, politics)
- Output method: terminal, file, or web dashboard (TBD)
- Eventual auto-trading vs. read-only research
