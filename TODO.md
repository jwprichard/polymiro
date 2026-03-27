# TODO

Improvements identified to increase prediction quality and pipeline reliability.

## Prediction Quality

- [ ] **Fix probability prompt calibration** (`mirofish/neo4j_query.py:187`) — include the
  market's current YES price, the timeframe from the FetchPlan, and an explicit instruction
  to avoid anchoring to 0.5. Currently the LLM has no price context and no anti-bias nudge.

- [ ] **Replace crypto_prices stub** (`research/source_router.py:25`) — `crypto_prices` maps
  to `WebFetcher` (generic scraper). Implement `CryptoFetcher` backed by CoinGecko so crypto
  markets get real price data.

- [ ] **Eliminate duplicate topic classification** (`scanner/opportunity_scorer.py`,
  `research/query_interpreter.py`) — the scorer and QueryInterpreter independently classify
  the same question. The scorer's `data_sources_suggested` output is ignored by the research
  agent. Either pass it through or remove it from the scorer.

## Pipeline Reliability

- [ ] **Build the main.py orchestrator loop** — no continuous runner exists. Scanner and
  research agent are one-shot scripts. Need a loop: scan → research top N → sleep → repeat.

- [ ] **Expire the research queue** (`research/research_agent.py:125`) — markets are never
  re-researched once queued. Add a TTL (e.g. 24h) or re-queue markets when their score
  changes significantly.

- [ ] **Batch or cache scan-time LLM calls** (`scanner/opportunity_scorer.py:98`) —
  `_classify_topic_ollama()` fires one Ollama call per market. For 100+ markets this
  serialises into dozens of slow calls. Batch or use the keyword heuristic path at scan
  time and reserve the LLM for the research phase.

## Scoring

- [ ] **Rethink the spread signal weight** (`scanner/opportunity_scorer.py:25`) — `WEIGHT_SPREAD`
  is 0.35 (highest) but the input is the bid-ask spread, not true edge. True mispricing edge
  can't be known until after research. Consider using bid-ask spread purely as a liquidity
  signal and rebalancing weights accordingly.
