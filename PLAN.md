# Polymarket × MiroFish Integration Plan

## Vision

A pipeline where autonomous agents scan Polymarket for mispriced or high-opportunity markets,
dispatch research agents to gather real-world data, feed that data into MiroFish to build a
knowledge graph, then query that graph directly for a probability estimate — bypassing MiroFish's
social simulation layer entirely.

---

## How We Use MiroFish (Important Clarification)

MiroFish's built-in output is a **social simulation** — it models how Twitter/Reddit agents would
discuss your documents. That is not useful for probability prediction.

**We use MiroFish only for what it's good at: knowledge graph construction.**

```
MiroFish role:  documents → Neo4j knowledge graph (entities, relationships, facts)
Our addition:   Neo4j graph → LLM probability query → YES/NO probability + reasoning
```

The social simulation step (`/api/simulation/start`) is never called. We stop after the graph
is built (`POST /ontology/generate` → graph ready) and query Neo4j directly.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        PHASE 1 (MVP)                            │
│                                                                 │
│  [Scanner Agent]                                                │
│   polymarket-cli → markets list, prices, spreads, volume        │
│        ↓                                                        │
│  [Opportunity File]  ← shared state (opportunities.json)        │
│        ↓                                                        │
│  [Research Agent]                                               │
│   reads opportunity → fetches real-world data (APIs/web)        │
│        ↓                                                        │
│  [MiroFish Ingest]                                              │
│   uploads fetched docs → builds Neo4j knowledge graph           │
│        ↓                                                        │
│  [Graph Probability Query]  ← NEW: replaces simulation          │
│   LLM queries Neo4j graph → YES probability + key factors       │
│        ↓                                                        │
│  [Output]                                                       │
│   probability vs Polymarket price → edge score → user reviews   │
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
# input:  "Will Bitcoin exceed $100k before April 2025?"
# output:
{
  "topic": "crypto",
  "entities": ["Bitcoin", "BTC"],
  "timeframe": "before April 2025",
  "sources": ["coingecko", "news_search", "on_chain_data"]
}
```

- Single LLM call (Ollama / qwen2.5 already running for MiroFish)
- Returns a `FetchPlan` dataclass

### 1.2 Fetcher Connectors

Each connector returns a list of plain-text documents ready for MiroFish ingestion.

| Connector | Source | Auth needed |
|---|---|---|
| `WeatherFetcher` | Open-Meteo API | None (free) |
| `NewsFetcher` | Tavily API or SerpAPI | API key |
| `WikiFetcher` | Wikipedia REST API | None |
| `WebFetcher` | requests + BeautifulSoup | None |
| `CryptoFetcher` | CoinGecko API | None (free tier) |
| `MacroFetcher` | FRED API | Free key |

Each connector writes results to `./fetched_docs/{run_id}/` as `.txt` files.

### 1.3 MiroFish Bridge (Graph Build Only)

Thin wrapper that POSTs fetched docs to MiroFish's graph construction endpoints only.
The simulation is never started.

```python
# Step 1: build the ontology + knowledge graph
POST /ontology/generate   # multipart form: files + market question as context
GET  /data/{graph_id}     # poll until graph is ready

# Step 2: stop here — do NOT call /api/simulation/start
# Hand off graph_id to the Graph Probability Query layer
```

Returns `graph_id` for downstream querying.

---

## Phase 2: Graph Probability Query Layer

**Goal:** Replace MiroFish's social simulation with a direct LLM probability query over the Neo4j graph.

### 2.1 How It Works

Once the knowledge graph is built in Neo4j, we query it for all relevant entities and
relationships, then pass that structured context to an LLM with a probability prompt.

```python
def query_graph_for_probability(graph_id: str, market_question: str) -> PredictionResult:
    # 1. Pull relevant nodes + edges from Neo4j
    nodes, edges = neo4j_client.get_graph(graph_id)

    # 2. Summarise into a context string
    context = format_graph_as_context(nodes, edges)

    # 3. Ask the LLM for a probability estimate
    prompt = f"""
    You are a prediction market analyst. Based only on the following knowledge graph
    derived from real-world data sources, estimate the probability that the following
    statement resolves YES.

    Question: {market_question}

    Knowledge Graph Context:
    {context}

    Respond with:
    - yes_probability: float between 0.0 and 1.0
    - confidence: low | medium | high
    - key_factors_for_yes: list of up to 5 supporting facts from the graph
    - key_factors_for_no: list of up to 5 opposing facts from the graph
    - reasoning: 2-3 sentence explanation
    """

    return llm.query(prompt, response_format=PredictionResult)
```

### 2.2 Neo4j Query

```cypher
// Pull all entities and relationships relevant to a graph_id
MATCH (e:Entity {graph_id: $graph_id})-[r]->(e2:Entity)
RETURN e.name, e.type, type(r), r.weight, e2.name, e2.type
LIMIT 200
```

Vector search is also available for semantic filtering if the graph is large.

### 2.3 PredictionResult Schema

```python
@dataclass
class PredictionResult:
    yes_probability: float          # 0.0 – 1.0
    confidence: str                 # "low" | "medium" | "high"
    key_factors_for_yes: list[str]
    key_factors_for_no: list[str]
    reasoning: str
    graph_id: str
    nodes_used: int
    edges_used: int
```

---

## Phase 3: Polymarket Scanner Agent

**Goal:** Continuously scan Polymarket for markets worth researching.

### 3.1 Market Scanner

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

### 3.2 Opportunity File (Shared State)

Agents communicate via a structured JSON file:

```
data/
  opportunities.json       ← scanner writes here
  research_queue.json      ← research agents consume from here
  results/
    {market_id}.json       ← graph probability predictions per market
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

### 3.3 Opportunity Scorer

```python
def score_opportunity(market: Market) -> float:
    spread_score    = normalize(market.spread)
    liquidity_score = 1 / log(market.volume + 1)
    urgency_score   = 1 / days_until_close(market)
    fetchability    = topic_has_real_data(market.question)  # LLM classifier
    return weighted_sum(spread_score, liquidity_score, urgency_score, fetchability)
```

---

## Phase 4: Research Agent Loop

**Goal:** Research agents read from `opportunities.json`, fetch data, build graph, query probability.

### 4.1 Research Agent Loop

```
1. Read opportunities.json → pick top-N unresearched markets
2. For each market:
   a. Query Interpreter → FetchPlan
   b. Run fetchers → write docs to fetched_docs/{market_id}/
   c. POST to MiroFish → build knowledge graph → get graph_id
   d. Query Neo4j graph for probability → PredictionResult
   e. Compute edge = our_yes_probability - polymarket_yes_price
   f. Write result to results/{market_id}.json
3. Mark market as researched in research_queue.json
4. Sleep → repeat on interval (e.g. every 30 min)
```

### 4.2 Source Routing by Market Type

| Market topic | Fetchers used |
|---|---|
| Weather | Open-Meteo, NOAA RSS |
| Sports | ESPN API, news search |
| Politics/Elections | News search, Wikipedia, polling APIs |
| Crypto | CoinGecko API, on-chain data, news |
| Macro/Economics | FRED API, news search |
| Science/Tech | arXiv, Wikipedia, news search |

### 4.3 Result Schema

```json
{
  "market_id": "0xabc...",
  "question": "Will Bitcoin exceed $100k before April 2025?",
  "graph_id": "graph_789",
  "prediction": {
    "yes_probability": 0.28,
    "confidence": "medium",
    "key_factors_for_yes": ["ETF approval momentum", "historical halving pattern"],
    "key_factors_for_no": ["hash rate declining", "ETF outflows accelerating"],
    "reasoning": "On-chain data shows weakening accumulation. Recent ETF outflows suggest institutional cooling.",
    "nodes_used": 84,
    "edges_used": 127
  },
  "polymarket_current_yes": 0.34,
  "edge": -0.06,
  "researched_at": "2025-03-26T10:30:00Z"
}
```

`edge` = our predicted probability - Polymarket current price.
- Positive edge → market underpricing YES → potential long opportunity
- Negative edge → market overpricing YES → potential short opportunity

---

## Phase 5: OpenViking Integration (Future)

**Goal:** Agents accumulate knowledge across sessions, reducing redundant fetches.

### What OpenViking Adds

- **Persistent memory**: Past graph results and predictions don't disappear between runs
- **Token efficiency**: L0/L1/L2 tiered loading instead of re-ingesting everything
- **Cross-market learning**: "Last time we researched Bitcoin markets, these sources were most predictive"
- **Session compression**: Long research sessions auto-compressed into retrievable memories

### Integration Points

```
Research Agent
  ├── Before fetch: check OpenViking for cached data on this topic
  ├── After graph query: store PredictionResult in OpenViking memory
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
│   ├── macro_fetcher.py        # FRED API
│   └── base_fetcher.py         # Abstract base class
│
├── mirofish/
│   ├── bridge.py               # MiroFish API client (graph build only)
│   └── neo4j_query.py          # Graph → probability query
│
├── research/
│   ├── research_agent.py       # Main research loop
│   └── source_router.py        # Topic → fetcher selection
│
├── data/
│   ├── opportunities.json      # Scanner output
│   ├── research_queue.json     # Research agent state
│   └── results/                # Per-market probability predictions
│
├── config.py                   # API keys, MiroFish URL, Neo4j URL, intervals
└── main.py                     # Entry point / orchestrator
```

---

## Build Order (Suggested)

| Step | What | Why first |
|---|---|---|
| 1 | `polymarket_client.py` + scanner shell | Proves market data is flowing |
| 2 | `opportunities.json` writer | Establishes shared state contract |
| 3 | `WeatherFetcher` + MiroFish bridge (graph only) | End-to-end graph build with simplest data |
| 4 | `neo4j_query.py` probability layer | Core new capability — validate this works |
| 5 | `query_interpreter.py` | Generalizes fetcher selection |
| 6 | Full `research_agent.py` loop | Wires everything together |
| 7 | Remaining fetchers (news, crypto, macro) | Broadens market coverage |
| 8 | OpenViking integration | Add after core loop is validated |

---

## Dependencies

```
# Python
requests
beautifulsoup4
tavily-python        # or serpapi
ollama               # already used by MiroFish
neo4j                # Python driver for direct Neo4j queries

# External services (free tier available)
Open-Meteo           # weather, no key needed
Wikipedia API        # no key needed
Tavily API           # news/web search, free tier
CoinGecko API        # crypto, free tier
FRED API             # macro data, free key

# Already required
polymarket-cli       # Rust binary, pre-installed
MiroFish             # running locally (used for graph build only)
Neo4j 5.15           # running locally
Ollama               # running locally (qwen2.5 for graph query + interpreter)
```

---

## Open Questions

1. **Run cadence**: How often should the scanner run? (suggest: every 30 min)
2. **Market filter**: Only scan specific tags (crypto, weather, sports) or all markets?
3. **Edge threshold**: What minimum edge score triggers a result worth surfacing? (suggest: ±0.05)
4. **Polymarket wallet**: Read-only research only, or eventually auto-trade on high-confidence signals?
5. **Notification**: How should results surface — terminal output, a file, or a simple web dashboard?
