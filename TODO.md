# TODO

Improvements identified to increase prediction quality and pipeline reliability.

## Prediction Quality

- [ ] **[P0] Research both sides of binary race markets** — the pipeline fetches documents
  supporting one interpretation (e.g. "Rihanna hype") but never explicitly researches the
  competing side (e.g. GTA VI release date). For any market framed as "X before Y", the
  research prompt and FetchPlan should require evidence on *both* X and Y independently,
  then reason about the race explicitly. Validated by manual check: system predicted 75%
  YES on "Rihanna album before GTA VI?" when the correct estimate is ~12% (GTA VI has a
  hard Nov 19 2026 date; Rihanna has no singles, no rollout, no release date).

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

## Updown Signal Enhancements (not implemented — future iteration)

The following signals are planned additions to the updown package to improve edge
calculation accuracy. None are implemented yet.

- [ ] **Orderbook imbalance weighting** — measure the bid/ask volume ratio at the top N price levels and scale the raw edge by the imbalance factor, amplifying the signal when heavy orderbook support aligns with the predicted direction.
- [ ] **Funding rate integration** — ingest the perpetual swap funding rate and add it as a signed offset to the edge, since a deeply negative funding rate implies crowded shorts and increases the probability of an upward move (and vice versa).
- [ ] **Volatility regime detection (ATR-based)** — compute the Average True Range over a rolling window to classify the current regime as low, normal, or high volatility, then widen or tighten the minimum edge threshold required before the executor acts.
- [ ] **Liquidation level proximity** — estimate clustered liquidation price levels from open interest data and boost the edge magnitude when the current price is near a liquidation cluster, since cascading liquidations accelerate directional moves.
- [ ] **Volume spike detection** — compare real-time trade volume against a rolling baseline and flag abnormal spikes as a conviction multiplier on the edge, treating sudden volume surges as confirmation of the predicted directional bias.
