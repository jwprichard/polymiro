# Polymarket × MiroFish Integration Plan

## Vision

A pipeline where autonomous agents scan Polymarket for mispriced or high-opportunity markets,
dispatch research agents to gather real-world data, feed that data into MiroFish for graph-based
simulation, and surface predictions back to the user.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        PHASE 1 (MVP)                            │
│                                                                 │
│  [Scanner Agent]                                                │
│   polymarket-cli → markets list, prices, spreads, volume        │
│        ↓                                                        │
│  [Opportunity File]  ← shared state (opportunities.json)       │
│        ↓                                                        │
│  [Research Agent]                                               │
│   reads opportunity → fetches real-world data (APIs/web)        │
│        ↓                                                        │
│  [MiroFish Ingest]                                              │
│   uploads fetched docs → builds knowledge graph → simulation    │
│        ↓                                                        │
│  [Output]                                                       │
│   prediction + confidence → user reviews                        │
└─────────────────────────────────────────────────────────────────┘

                    PHASE 2 (Future)
              Add OpenViking memory layer
              Agents learn across sessions
              Auto-position sizing suggestions
```

---

## Phase 1: Auto-Fetch Layer for MiroFish

**Goal:** Replace manual document uploads with autonomous data fetching.

### 1.1 Query Interpreter

A small module that takes a free-text question and returns structured fetch instructions.

```python
# input:  "Will it rain in London in 5 days?"
# output:
{
  "topic": "weather",
  "entities": ["London"],
  "timeframe": "5 days",
  "sources": ["open_meteo", "news_search"]
}
```

- Single LLM call (Ollama / qwen2.5 already running for MiroFish)
- Returns a `FetchPlan` dataclass

### 1.2 Fetcher Connectors

Each connector returns a list of plain-text documents ready for MiroFish.

| Connector | Source | Auth needed |
|---|---|---|
| `WeatherFetcher` | Open-Meteo API | None (free) |
| `NewsFetcher` | Tavily API or SerpAPI | API key |
| `WikiFetcher` | Wikipedia REST API | None |
| `WebFetcher` | requests + BeautifulSoup | None |

Each connector writes results to `./fetched_docs/{run_id}/` as `.txt` files.

### 1.3 MiroFish Bridge

Thin wrapper that POSTs fetched docs to MiroFish's existing endpoints:
- `POST /ontology/generate` — multipart form with files + simulation prompt
- `GET /data/{graph_id}` — poll until graph is ready
- Returns graph ID for downstream use

### 1.4 New UI Entry Point (optional)

Add a text input to `Home.vue` alongside the existing drag-and-drop:
```
"What do you want to predict?" [____________] [Go]
```
Calls the query interpreter → fetcher → bridge pipeline automatically.

---

## Phase 2: Polymarket Scanner Agent

**Goal:** Continuously scan Polymarket for markets worth researching.

### 2.1 Market Scanner

Uses `polymarket-cli` (JSON output mode) to pull live market data:

```bash
polymarket markets list --active true --order volume_num --limit 100 --output json
polymarket clob spread <TOKEN_ID> --output json
polymarket clob price-history <TOKEN_ID> --interval 1h --output json
```

Scoring criteria for "opportunity":
- **Wide spread** relative to market volume (potential mispricing)
- **Low liquidity** on one side (edge for informed traders)
- **Rapidly shifting prices** (new information not yet priced in)
- **Upcoming resolution date** (short time horizon = faster feedback)
- **Topic overlap** with data we can actually fetch (weather, sports, politics, crypto)

### 2.2 Opportunity File (Shared State)

Agents communicate via a structured JSON file:

```
data/
  opportunities.json       ← scanner writes here
  research_queue.json      ← research agents consume from here
  results/
    {market_id}.json       ← MiroFish predictions per market
```

**`opportunities.json` schema:**
```json
[
  {
    "market_id": "0xabc...",
    "question": "Will Bitcoin exceed $100k before April 2025?",
    "current_yes_price": 0.34,
    "current_no_price": 0.68,
    "volume_24h": 45000,
    "spread": 0.02,
    "closes_at": "2025-04-01T00:00:00Z",
    "opportunity_score": 0.82,
    "data_sources_suggested": ["crypto_prices", "news_search", "on_chain_data"],
    "scanned_at": "2025-03-26T10:00:00Z"
  }
]
```

### 2.3 Opportunity Scorer

A scoring function that ranks markets by research ROI:

```python
def score_opportunity(market: Market) -> float:
    spread_score    = normalize(market.spread)
    liquidity_score = 1 / log(market.volume + 1)
    urgency_score   = 1 / days_until_close(market)
    fetchability    = topic_has_real_data(market.question)  # LLM classifier
    return weighted_sum(spread_score, liquidity_score, urgency_score, fetchability)
```

---

## Phase 3: Research Agent Layer

**Goal:** Research agents read from `opportunities.json`, autonomously fetch data, feed MiroFish.

### 3.1 Research Agent Loop

```
1. Read opportunities.json → pick top-N unresearched markets
2. For each market:
   a. Query Interpreter → FetchPlan
   b. Run fetchers → write docs to fetched_docs/{market_id}/
   c. POST to MiroFish → get graph_id
   d. Poll for simulation result
   e. Write result to results/{market_id}.json
3. Mark market as researched in research_queue.json
4. Sleep → repeat on interval (e.g. every 30 min)
```

### 3.2 Source Routing by Market Type

| Market topic | Fetchers used |
|---|---|
| Weather | Open-Meteo, NOAA RSS |
| Sports | ESPN API, news search |
| Politics/Elections | News search, Wikipedia, polling APIs |
| Crypto | CoinGecko API, on-chain data, news |
| Macro/Economics | FRED API (free), news search |
| Science/Tech | arXiv, Wikipedia, news search |

### 3.3 Result Schema

```json
{
  "market_id": "0xabc...",
  "question": "Will Bitcoin exceed $100k before April 2025?",
  "mirofish_graph_id": "graph_789",
  "prediction": {
    "yes_probability": 0.28,
    "confidence": "medium",
    "key_factors": ["hash rate declining", "ETF outflows accelerating"],
    "data_sources_used": ["coingecko", "news_tavily"]
  },
  "polymarket_current_yes": 0.34,
  "edge": -0.06,
  "researched_at": "2025-03-26T10:30:00Z"
}
```

`edge` = MiroFish prediction - Polymarket current price. Positive = market underpricing YES.

---

## Phase 4: OpenViking Integration (Future)

**Goal:** Agents accumulate knowledge across sessions, reducing redundant fetches.

### What OpenViking Adds

- **Persistent memory**: Past MiroFish results don't disappear between runs
- **Token efficiency**: L0/L1/L2 tiered loading instead of re-ingesting everything
- **Cross-market learning**: "Last time we researched Bitcoin markets, these sources were most predictive"
- **Session compression**: Long research sessions auto-compressed into retrievable memories

### Integration Points

```
Research Agent
  ├── Before fetch: check OpenViking for cached data on this topic
  ├── After MiroFish: store result in OpenViking memory
  └── Between sessions: OpenViking recalls what was learned last time

Scanner Agent
  └── OpenViking stores market scoring history
      (prevents re-scoring markets we already researched with no edge)
```

### Storage Split

| Data | Stored in |
|---|---|
| Knowledge graph (entities, relationships) | Neo4j (MiroFish) |
| Agent memories, session context | OpenViking |
| Raw fetched documents | Local filesystem |
| Market opportunities & results | JSON files (shared state) |

---

## Project Structure

```
polymarket-mirofish/
├── scanner/
│   ├── scanner_agent.py        # Polymarket scanning loop
│   ├── opportunity_scorer.py   # Market scoring logic
│   └── polymarket_client.py    # Wraps polymarket-cli JSON output
│
├── fetchers/
│   ├── query_interpreter.py    # LLM → FetchPlan
│   ├── weather_fetcher.py      # Open-Meteo
│   ├── news_fetcher.py         # Tavily / SerpAPI
│   ├── wiki_fetcher.py         # Wikipedia API
│   ├── crypto_fetcher.py       # CoinGecko
│   └── base_fetcher.py         # Abstract base class
│
├── mirofish/
│   ├── bridge.py               # MiroFish API client
│   └── result_parser.py        # Parse simulation output
│
├── research/
│   ├── research_agent.py       # Main research loop
│   └── source_router.py        # Topic → fetcher selection
│
├── data/
│   ├── opportunities.json      # Scanner output
│   ├── research_queue.json     # Research agent state
│   └── results/                # Per-market MiroFish predictions
│
├── config.py                   # API keys, MiroFish URL, intervals
└── main.py                     # Entry point / orchestrator
```

---

## Build Order (Suggested)

| Step | What | Why first |
|---|---|---|
| 1 | `polymarket_client.py` + scanner shell | Proves data is flowing |
| 2 | `opportunities.json` writer | Establishes shared state contract |
| 3 | `WeatherFetcher` + MiroFish bridge | End-to-end with simplest data source |
| 4 | `query_interpreter.py` | Generalizes fetcher selection |
| 5 | Full `research_agent.py` loop | Wires everything together |
| 6 | Remaining fetchers (news, crypto, wiki) | Broadens market coverage |
| 7 | OpenViking integration | Add after core loop is validated |

---

## Dependencies

```
# Python
requests
beautifulsoup4
tavily-python        # or serpapi
ollama               # already used by MiroFish

# External services (free tier available)
Open-Meteo           # weather, no key needed
Wikipedia API        # no key needed
Tavily API           # news/web search, free tier
CoinGecko API        # crypto, free tier
FRED API             # macro data, free key

# Already required
polymarket-cli       # Rust binary, pre-installed
MiroFish             # running locally
Neo4j 5.15           # running locally
Ollama               # running locally
```

---

## Open Questions

1. **Run cadence**: How often should the scanner run? (suggest: every 30 min)
2. **Market filter**: Only scan specific tags (crypto, weather, sports) or all markets?
3. **MiroFish simulation prompt**: Should it be generic ("predict the probability of YES") or customized per market type?
4. **Polymarket wallet**: Do you want to eventually auto-trade on high-confidence predictions, or keep this read-only research only?
5. **Notification**: How should results surface — terminal output, a file, a simple web dashboard?
