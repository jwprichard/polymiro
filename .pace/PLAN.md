# PLAN: Build the Polymarket × MiroFish Research Pipeline
_Created: 2026-03-26T00:00:00Z_

## Objective
Build and wire together the full research layer — fetchers, MiroFish bridge, Neo4j query, LLM probability estimator, source router, query interpreter, and orchestration loop — so that `python3 -m research.research_agent` processes the top opportunity from `data/opportunities.json` and writes a complete `data/results/{market_id}.json`.

---

## Tasks

<!-- ─────────────────────────────────────────────
     WAVE 1  —  Prerequisites (run in parallel)
     ───────────────────────────────────────────── -->

### Task 1: Discover and install MiroFish locally  ⚠ CRITICAL PATH
**Agent:** @Backend Architect
**Depends on:** none
**Files:** `config.py`, `mirofish/bridge.py` (install-notes comment), `scripts/install_mirofish.sh` (if install steps needed)
**Allowed tools:** Read, Write, Edit, Bash, Glob, Grep
**Success criteria:**
- MiroFish HTTP service responds to a health-check request (e.g. `GET /health` or `GET /`) on `http://localhost:<PORT>`
- `POST /ontology/generate` with a multipart form containing at least one text file and a `question` field returns a JSON body that contains an id suitable for polling (the exact key — `graph_id`, `id`, or other — is noted in a comment at the top of `mirofish/bridge.py`)
- `GET /data/{graph_id}` returns a completion status and graph metadata once the build finishes
- The confirmed port and base URL are stored in `config.py` as `MIROFISH_BASE_URL` (env-overridable, default `http://localhost:8000`)
- If MiroFish was not already running, a `scripts/install_mirofish.sh` exists with the exact steps used to install and start it
- The comment block at the top of `mirofish/bridge.py` records: install source (PyPI package name, npm package, or GitHub URL), confirmed start command, confirmed API field names (`graph_id` key, status field, ready value)

### Task 2: Add research-layer config constants to config.py
**Agent:** @Backend Architect
**Depends on:** none
**Files:** `config.py`
**Allowed tools:** Read, Edit
**Success criteria:**
- `config.py` gains the following env-overridable constants without removing or renaming any existing constants:
  - `MIROFISH_BASE_URL` (default `"http://localhost:8000"`)
  - `MIROFISH_POLL_INTERVAL_S` (default `2.0`)
  - `MIROFISH_POLL_TIMEOUT_S` (default `120.0`) — inline comment notes that runs with many fetchers may need this increased
  - `NEO4J_URI` (default `"bolt://localhost:7687"`)
  - `NEO4J_USER` (default `"neo4j"`)
  - `NEO4J_PASSWORD` (default `"neo4j"`)
  - `FETCHED_DOCS_DIR` (default `Path(__file__).parent / "fetched_docs"`)
  - `RESULTS_DIR` (default `Path(__file__).parent / "data" / "results"`)
  - `TAVILY_API_KEY` (from env, default `""` — empty string means absent)
  - `RESEARCH_MIN_EDGE` (default `0.05`)
  - `LLM_PROVIDER` — already present; confirm it defaults to `"ollama"` and add an inline comment documenting the `"none"` short-circuit contract
- Running `python3 -c "import config"` exits 0

### Task 3: Install Python dependencies
**Agent:** @Backend Architect
**Depends on:** none
**Files:** TBD (system pip installs only)
**Allowed tools:** Bash
**Success criteria:**
- `pip3 install requests beautifulsoup4 tavily-python ollama neo4j` completes without error
- `python3 -c "from neo4j import GraphDatabase; import ollama; import bs4"` exits 0
- `python3 -c "import tavily"` exits 0 (confirming optional dependency is present; pipeline must not crash if it is later absent at runtime)

<!-- ─────────────────────────────────────────────
     WAVE 2  —  Base infrastructure (run in parallel; Tasks 4 and 5 depend only on Wave 1)
     Task 4 unblocks the entire fetcher wave.
     Task 5 depends on Task 1 confirmation of API contracts.
     ───────────────────────────────────────────── -->

### Task 4: Define BaseFetcher contract and create fetchers/ package skeleton
**Agent:** @Backend Architect
**Depends on:** 2, 3
**Files:** `fetchers/__init__.py`, `fetchers/base_fetcher.py`
**Allowed tools:** Read, Write, Edit
**Success criteria:**
- `fetchers/base_fetcher.py` defines `FetcherError(RuntimeError)` and `BaseFetcher` as an abstract base class with:
  - `__init__(self, run_id: str)` — stores `run_id`; computes `output_dir = config.FETCHED_DOCS_DIR / run_id` and calls `output_dir.mkdir(parents=True, exist_ok=True)`
  - `fetch(self, topic: str) -> list[Path]` — abstract; must return the list of `Path` objects written
  - `_write_doc(self, filename: str, content: str) -> Path` — concrete helper; writes `content` as UTF-8 to `output_dir / filename`, returns the absolute `Path`
- `fetchers/__init__.py` is present and importable (may be initially empty; exports are completed in Task 9)
- `python3 -c "from fetchers import BaseFetcher"` exits 0

### Task 5: Build MiroFish bridge (graph build + polling)
**Agent:** @Backend Architect
**Depends on:** 1, 2, 3
**Files:** `mirofish/__init__.py`, `mirofish/bridge.py`
**Allowed tools:** Read, Write, Edit, Bash, Glob, Grep
**Success criteria:**
- `mirofish/bridge.py` exposes `build_graph(question: str, doc_paths: list[Path]) -> str` which:
  1. POSTs a multipart form to `{MIROFISH_BASE_URL}/ontology/generate` — one `files` field per path, one `question` text field
  2. Extracts the graph id from the response JSON using the confirmed key from Task 1 (normalised to `graph_id` internally)
  3. Polls `GET {MIROFISH_BASE_URL}/data/{graph_id}` every `MIROFISH_POLL_INTERVAL_S` seconds until the confirmed ready status is returned, or until `MIROFISH_POLL_TIMEOUT_S` is exceeded
  4. Returns `graph_id` on success; raises `MiroFishError(RuntimeError)` on timeout, poll exhaustion, or any HTTP error — never leaks `requests.RequestException` to callers
- `/api/simulation/start` is never referenced anywhere in this module; a module-level comment explicitly marks this as architecturally forbidden
- `mirofish/__init__.py` exports `build_graph` and `MiroFishError`
- `python3 -m mirofish.bridge` smoke test calls `build_graph` with a single temp file and a test question; prints the returned `graph_id`

<!-- ─────────────────────────────────────────────
     WAVE 3  —  Fetcher implementations (independent of each other; all depend on Task 4)
     Tasks 6–9 can run fully in parallel.
     ───────────────────────────────────────────── -->

### Task 6: Implement WeatherFetcher
**Agent:** @Backend Architect
**Depends on:** 4
**Files:** `fetchers/weather_fetcher.py`
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- `WeatherFetcher(run_id).fetch(topic)` calls the Open-Meteo geocoding API to resolve `topic` to lat/lon (falls back to a default location when no city is detected), then calls the Open-Meteo forecast endpoint; writes one plain-text file named `weather_{topic_slug}.txt` containing current conditions and a 7-day daily summary
- No API key required; uses only `requests`
- Raises `FetcherError` on network failure rather than propagating `requests.RequestException` raw
- Runnable standalone: `python3 -m fetchers.weather_fetcher "London"` writes a file under `fetched_docs/standalone_{date}/` and prints its path

### Task 7: Implement WikiFetcher
**Agent:** @Backend Architect
**Depends on:** 4
**Files:** `fetchers/wiki_fetcher.py`
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- `WikiFetcher(run_id).fetch(topic)` calls Wikipedia REST API `/api/rest_v1/page/summary/{title}`; falls back to the Wikipedia search API (`/w/api.php?action=query&list=search`) when a direct title lookup returns 404; writes page title, extract (first 3 000 characters), and source URL as plain text in `wiki_{topic_slug}.txt`
- No API key required
- Raises `FetcherError` on network failure
- Runnable standalone: `python3 -m fetchers.wiki_fetcher "Bitcoin"` writes a file and prints its path

### Task 8: Implement WebFetcher
**Agent:** @Backend Architect
**Depends on:** 4
**Files:** `fetchers/web_fetcher.py`
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- `WebFetcher(run_id).fetch(topic)` accepts a URL; uses `requests` + `BeautifulSoup` to extract visible text from `<p>` tags; strips boilerplate elements (`nav`, `footer`, `script`, `style`); truncates output to 5 000 characters; writes `web_{url_slug}.txt`
- Gracefully skips on timeout or non-200 response (logs warning, returns `[]`)
- Raises `FetcherError` on unexpected exceptions
- Runnable standalone: `python3 -m fetchers.web_fetcher "https://example.com"` writes a file and prints its path

### Task 9: Implement NewsFetcher (graceful degradation)
**Agent:** @Backend Architect
**Depends on:** 4
**Files:** `fetchers/news_fetcher.py`
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- `NewsFetcher(run_id).fetch(topic)` checks `config.TAVILY_API_KEY` at call time (not import time); if absent or empty, logs a `WARNING` and returns `[]` without raising
- When key is present, calls the Tavily search API (`TavilyClient.search(query=topic, max_results=5)`); writes up to 5 plain-text files named `news_{topic_slug}_{n}.txt`, each containing title, URL, and content snippet
- The `tavily` import is wrapped in a `try/except ImportError` so the module is importable even if `tavily-python` is not installed
- Raises `FetcherError` only for unexpected Tavily API errors (not for missing key or missing package)
- Runnable standalone: `python3 -m fetchers.news_fetcher "inflation"` exits 0 even when `TAVILY_API_KEY` is absent

<!-- ─────────────────────────────────────────────
     WAVE 3 (continued)  —  Neo4j query layer + graph formatter
     Task 10 depends on Task 2 (config) and Task 3 (deps) only; independent of MiroFish bridge.
     ───────────────────────────────────────────── -->

### Task 10: Build neo4j_query.py — raw graph query and context formatter
**Agent:** @Backend Architect
**Depends on:** 2, 3
**Files:** `mirofish/neo4j_query.py`
**Allowed tools:** Read, Write, Edit, Bash, Grep
**Success criteria:**
- `mirofish/neo4j_query.py` exposes:
  - `query_graph(graph_id: str) -> list[dict]` — opens a `neo4j.GraphDatabase.driver` session using `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`; runs the canonical Cypher: `MATCH (e:Entity {graph_id: $graph_id})-[r]->(e2:Entity) RETURN e.name, e.type, type(r), r.weight, e2.name, e2.type LIMIT 200`; returns raw record dicts; raises `Neo4jQueryError(RuntimeError)` on connection or query failure; closes the driver in a `finally` block
  - `format_graph_as_context(rows: list[dict]) -> str` — converts rows to a human-readable string, one relationship per line: `{e.name} ({e.type}) --[{rel_type} w={weight}]--> {e2.name} ({e2.type})`; sorts by `r.weight` descending so the most strongly-weighted relationships appear first; enforces a hard cap of 6 000 characters, appending `... [truncated]` if exceeded; returns `""` for an empty row list
- `python3 -m mirofish.neo4j_query` runs with a dummy `graph_id`, prints row count (0 rows is acceptable — proves connectivity)

<!-- ─────────────────────────────────────────────
     WAVE 4  —  LLM layer (owned by @AI Engineer)
     Task 11 depends on Tasks 2, 3 (no MiroFish or fetcher dependency).
     Task 12 depends on Task 10 (graph formatter output) and Tasks 2, 3.
     Tasks 11 and 12 are independent of each other and can run in parallel.
     ───────────────────────────────────────────── -->

### Task 11: Build research/query_interpreter.py (FetchPlan + Ollama integration)
**Agent:** @AI Engineer
**Depends on:** 2, 3
**Files:** `research/__init__.py`, `research/query_interpreter.py`, `research/_llm_utils.py`
**Allowed tools:** Read, Write, Edit, Grep
**Success criteria:**
- `FetchPlan` dataclass fields: `topic: str`, `entities: list[str]`, `timeframe: str`, `sources: list[str]`, `queries: dict[str, str]` (fetcher name → concrete search string)
- `research/_llm_utils.py` provides `ollama_json_call(messages: list[dict], model: str, max_retries: int = 2) -> dict` — shared retry utility for Ollama JSON calls; retries with a correction follow-up message on JSON parse failure; returns the parsed dict or raises after exhausting retries
- `QueryInterpreter.interpret(market_question: str) -> FetchPlan`:
  - When `LLM_PROVIDER == "none"`: returns a `FetchPlan` derived from keyword heuristics (no Ollama call); keywords like `"bitcoin"` / `"crypto"` → `sources=["wiki", "web"]`; always returns a valid plan
  - When `LLM_PROVIDER == "ollama"`: calls `ollama_json_call` with a structured system prompt that includes a one-shot example of the full FetchPlan JSON schema; parses and validates the response; falls back to keyword heuristic on any error (`except Exception: # noqa: BLE001`)
- `research/__init__.py` exists and is importable
- `interpret()` never raises to its caller in any code path

### Task 12: Build mirofish/neo4j_query.py — probability estimator
**Agent:** @AI Engineer
**Depends on:** 10
**Files:** `mirofish/neo4j_query.py`
**Allowed tools:** Read, Edit
**Success criteria:**
- `estimate_probability(graph_context: str, market_question: str, node_count: int = 0, edge_count: int = 0, doc_paths: list[Path] | None = None) -> tuple[float, str]` is added to `mirofish/neo4j_query.py`:
  - When `LLM_PROVIDER == "none"`: immediately returns `(0.5, "LLM disabled — neutral estimate")` without calling Ollama
  - When `graph_context` is empty and `doc_paths` is provided: constructs a fallback prompt from raw document text (truncated to 6 000 characters)
  - Otherwise: sends an Ollama `chat` call to `config.OLLAMA_MODEL` via `ollama_json_call`; system prompt: `"You are a prediction market analyst. Respond with only a JSON object — no markdown, no prose."`; user message includes the market question and formatted graph context; instructs the model to return `{"probability": <float 0.0–1.0>, "reasoning": "<2–3 sentences>"}`
  - On second parse failure after retry: returns `(0.5, "LLM parse error — defaulting to neutral")`
- Returns `(float, str)` in every code path; never raises to caller

<!-- ─────────────────────────────────────────────
     WAVE 5  —  Routing layer
     Task 13 depends on Task 4 (BaseFetcher) and Task 11 (FetchPlan definition).
     ───────────────────────────────────────────── -->

### Task 13: Build research/source_router.py
**Agent:** @Backend Architect
**Depends on:** 4, 11
**Files:** `research/source_router.py`
**Allowed tools:** Read, Write, Edit, Grep
**Success criteria:**
- `route(opportunity_sources: list[str]) -> list[str]` returns a list of fetcher class name strings (e.g. `["WikiFetcher", "NewsFetcher"]`) mapped from source label strings using a static registry dict:
  - `"wikipedia"` → `"WikiFetcher"`
  - `"weather"` → `"WeatherFetcher"`
  - `"news_search"` → `"NewsFetcher"`
  - `"web_search"` → `"WebFetcher"`
  - `"crypto_prices"` → `"WebFetcher"` (stub; `CryptoFetcher` is a future replacement)
  - all unrecognised labels → `"NewsFetcher"` as fallback
- Always returns at least `["WikiFetcher", "WebFetcher"]` when the mapped list would otherwise be empty
- When `LLM_PROVIDER == "none"`, routing uses only the source labels passed in — no Ollama call is ever made inside this module

<!-- ─────────────────────────────────────────────
     WAVE 6  —  Fetcher package finalisation (depends on all fetcher impls)
     ───────────────────────────────────────────── -->

### Task 14: Finalise fetchers/__init__.py exports and standalone entry points
**Agent:** @Backend Architect
**Depends on:** 6, 7, 8, 9
**Files:** `fetchers/__init__.py`, `fetchers/weather_fetcher.py`, `fetchers/wiki_fetcher.py`, `fetchers/web_fetcher.py`, `fetchers/news_fetcher.py`
**Allowed tools:** Read, Edit
**Success criteria:**
- `fetchers/__init__.py` exports: `BaseFetcher`, `FetcherError`, `WeatherFetcher`, `WikiFetcher`, `WebFetcher`, `NewsFetcher`
- Each concrete fetcher module has an `if __name__ == "__main__":` block that accepts a topic or URL as `sys.argv[1]`, constructs the fetcher with `run_id = f"standalone_{date.today()}"`, calls `fetch(sys.argv[1])`, and prints the paths of files written
- `python3 -c "from fetchers import WeatherFetcher, WikiFetcher, WebFetcher, NewsFetcher, FetcherError"` exits 0

<!-- ─────────────────────────────────────────────
     WAVE 7  —  Orchestration loop (convergence point)
     Task 15 depends on all fetcher class names (Task 14), the bridge (Task 5),
     the neo4j layer (Tasks 10 + 12), the router (Task 13), and the interpreter (Task 11).
     ───────────────────────────────────────────── -->

### Task 15: Build research/research_agent.py orchestration loop
**Agent:** @Backend Architect
**Depends on:** 5, 10, 11, 12, 13, 14
**Files:** `research/research_agent.py`
**Allowed tools:** Read, Write, Edit, Bash, Glob, Grep
**Success criteria:**
- `process_top_opportunity()` implements the full pipeline:
  1. Reads `data/opportunities.json`; if missing or empty, logs a warning and exits cleanly (exit code 0)
  2. Loads `data/research_queue.json` (treats missing file as `[]`)
  3. Selects the highest-scoring opportunity not already in the research queue
  4. Calls `QueryInterpreter().interpret(question)` → `FetchPlan`
  5. Calls `route(fetch_plan.sources)` → list of fetcher class name strings; instantiates each with `run_id = market_id`; calls `fetch(topic)` for each topic in `fetch_plan.queries[fetcher_name]`; collects all returned `Path` objects
  6. Calls `mirofish.build_graph(question, doc_paths)` → `graph_id`; on `MiroFishError`, logs the error, sets `graph_id = None`, and continues with `graph_context = ""`
  7. Calls `mirofish.neo4j_query.query_graph(graph_id)` → rows (skipped when `graph_id` is None); calls `format_graph_as_context(rows)` → `graph_context`
  8. Calls `estimate_probability(graph_context, question, doc_paths=doc_paths)` → `(predicted_probability, evidence_summary)`
  9. Computes `edge = predicted_probability − opportunity.current_yes_price`
  10. Creates `config.RESULTS_DIR` and `config.RESULTS_DIR.parent` with `mkdir(parents=True, exist_ok=True)` before writing
  11. Writes result atomically via `.tmp.json` + `os.replace()` to `data/results/{market_id}.json` with fields: `market_id`, `question`, `predicted_probability`, `edge`, `evidence_summary`, `graph_id`, `scanned_at` (copied from source opportunity, not a new timestamp)
  12. Appends `market_id` to `data/research_queue.json` atomically (read → append → write tmp → `os.replace()`) only after a successful result write
- `FetcherError`, `MiroFishError`, `Neo4jQueryError` are each caught and logged with a human-readable message; the agent exits with code 1 only if no result was written; MiroFish failure is recoverable (see step 6 above)
- `if __name__ == "__main__":` block calls `process_top_opportunity()` so `python3 -m research.research_agent` runs end-to-end
- `python3 -m research.research_agent` exits 0 and `data/results/{market_id}.json` is present with all required fields

<!-- ─────────────────────────────────────────────
     WAVE 8  —  Smoke tests (can run in parallel after Task 15)
     ───────────────────────────────────────────── -->

### Task 16: Write end-to-end pipeline smoke test
**Agent:** @Backend Architect
**Depends on:** 15
**Files:** `scripts/smoke_test_research.py`
**Allowed tools:** Read, Write, Edit, Bash
**Success criteria:**
- `LLM_PROVIDER=none python3 scripts/smoke_test_research.py` validates:
  1. `data/opportunities.json` is non-empty and parseable
  2. `process_top_opportunity()` runs without raising
  3. `data/results/{market_id}.json` exists and contains all required fields: `market_id`, `question`, `predicted_probability`, `edge`, `evidence_summary`, `graph_id`, `scanned_at`
  4. `predicted_probability` is a `float` in `[0.0, 1.0]`
  5. `edge` equals `predicted_probability − current_yes_price` within float tolerance (`abs(edge - (predicted_probability - current_yes_price)) < 1e-9`)
  6. `data/research_queue.json` contains the processed `market_id`
  7. No reference to `/api/simulation/start` exists anywhere under `mirofish/` (grep assertion)
- Script exits 0 on all checks passing, 1 on any failure, with one PASS/FAIL line per check printed to stdout

### Task 17: Write LLM integration smoke test
**Agent:** @AI Engineer
**Depends on:** 11, 12
**Files:** `scripts/smoke_test_llm.py`
**Allowed tools:** Read, Write, Edit, Bash
**Success criteria:**
- `LLM_PROVIDER=none python3 scripts/smoke_test_llm.py` runs without calling Ollama and exits 0
- Script calls `QueryInterpreter().interpret("Will Bitcoin exceed $100k before April 2026?")` and prints the resulting `FetchPlan` as JSON; asserts all required fields are present
- Script calls `estimate_probability(graph_context="Bitcoin price is $95k. ETF inflows strong.", market_question="Will Bitcoin exceed $100k?")` and prints the result; asserts `probability` is a float in `[0.0, 1.0]`
- Both fallback paths are exercised and confirmed reachable in `none` mode

---

## Notes

### Architectural decisions captured during synthesis

**MiroFish simulation guard (from all three drafts — unanimous).**
`/api/simulation/start` must never appear in the `mirofish/` package. This is enforced structurally (only `build_graph` is exported) and verified by a grep assertion in Task 16's smoke test.

**Responsibility split for `neo4j_query.py`.**
Three drafts disagreed on ownership. Decision: `query_graph()` and `format_graph_as_context()` are written by @Backend Architect in Task 10. `estimate_probability()` is added to the same file by @AI Engineer in Task 12. Task 12 must read the file before editing to avoid overwrite conflicts. No other module contains raw Cypher.

**`BaseFetcher` uses constructor-based `run_id`** (SA's approach), not per-call `run_id` (BA's and AI's). All fetcher calls are therefore `WeatherFetcher(run_id).fetch(topic)`. The research agent instantiates each fetcher with `run_id = market_id`.

**`source_router.route()` returns class name strings**, not instantiated objects (SA's approach). The research agent is responsible for instantiating each fetcher with the correct `run_id`. This keeps routing logic testable without side effects.

**`FetchPlan` uses AI Engineer's richer schema** (`topic`, `entities`, `timeframe`, `sources`, `queries`) since @AI Engineer owns the component. The `queries` dict (`fetcher_name → search string`) is load-bearing: the research agent uses it to select the right topic per fetcher.

**`LLM_PROVIDER="none"` is a first-class path**, not an afterthought. Every component that calls Ollama checks this flag. Task 16's smoke test runs with `LLM_PROVIDER=none` to confirm this end-to-end.

**`scanned_at` is inherited from the source opportunity**, not generated fresh at research time. This preserves the original scan timestamp.

**`evidence_summary` is a short string** (LLM reasoning from `estimate_probability`), not a list of filenames. The BA draft proposed filenames; the AI draft proposed reasoning text. Decision: reasoning text is more useful in `results/{market_id}.json` for human inspection. Filenames can be reconstructed from `fetched_docs/{market_id}/` on disk.

**`data/research_queue.json` append happens only after a successful result write.** Failed markets are not enqueued and will be retried on the next run. This is the correct behaviour for resilience.

**`NEO4J_PASSWORD` default is `"neo4j"`** (SA's value). BA's draft used `"password"` — overridden because `"neo4j"` is Neo4j's own factory default and is more likely to be correct for a fresh local install.

**Port 3000 is occupied** (Next.js app on the host). MiroFish must not use port 3000. Task 1 must confirm a free port and record it in `config.py`.

### Tasks dropped as duplicates

- **BA Task 8** (config constants) — superseded by Task 2 (SA's more complete version)
- **AI Task 1** (config constants) — same
- **BA Task 3 / AI Task 2** (BaseFetcher) — superseded by Task 4
- **AI Tasks 3–6** (fetcher implementations) — superseded by Tasks 6–9 (BA's implementations are more detailed on file naming, error types, and standalone entry points)
- **BA Task 14** (LLM_PROVIDER wire-up verification) — folded into Task 16 smoke test success criteria
- **BA Task 15** (data/results/ and queue init guard) — folded into Task 15 research_agent success criteria
- **BA Task 16 / AI Task 13** (smoke tests) — superseded by Tasks 16 and 17 respectively

### Gaps flagged (not covered by any draft)

- The existing `config.py` content is unknown to the plan authors; the implementing agent for Task 2 must read it before editing to avoid clobbering existing constants.
- `OLLAMA_MODEL` and `OLLAMA_HOST` are referenced by AI Engineer but not added in Task 2. If they do not already exist in `config.py`, Task 2's implementer should add them alongside the other new constants.
- `opportunity.current_yes_price` is used in Task 15's edge calculation — the field name must match the schema written by the existing scanner. The implementer should verify the `Opportunity` dataclass field name before writing the edge calculation.

### Scope deferred to future phases

- `CryptoFetcher` and `MacroFetcher` — mentioned in CLAUDE.md build order but out of scope for this phase. `source_router` maps `"crypto_prices"` → `"WebFetcher"` as a stub until a proper `CryptoFetcher` is ready.
- OpenViking integration (Phase 5 per CLAUDE.md) — out of scope.
- Auto-trading execution — out of scope; this phase is read-only research.
- `_llm_utils.py` shared retry utility — AI Engineer's draft suggests this as a refactor to avoid duplicating retry logic between `query_interpreter.py` and `estimate_probability`. Task 11 creates this file; Task 12 must import from it rather than duplicating the retry pattern.
