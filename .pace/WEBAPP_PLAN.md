# Webapp Implementation Plan
_Created: 2026-03-28_

## Architecture

```
Browser (React/Vite, port 5174)
    |  REST + SSE
    v
FastAPI backend (port 8001)
    |-- reads/writes  --> data/opportunities.json, data/results/*.json
    |-- spawns        --> scanner_agent.py, research_agent.py (subprocess)
    |-- executes      --> polymarket clob ... --output json (subprocess)
    |-- reads/writes  --> Postgres (trades, predictions, exit_signals, schedules)
    v
Postgres 16 (port 5432, internal only)
```

## New Files

### Backend (`backend/`)
```
backend/
├── Dockerfile
├── requirements.txt
├── main.py                        # FastAPI app factory, lifespan
├── db.py                          # SQLAlchemy async engine + session factory
├── models.py                      # ORM models
├── schemas.py                     # Pydantic request/response models
├── routers/
│   ├── opportunities.py           # GET /api/opportunities
│   ├── predictions.py             # GET /api/predictions, sync
│   ├── agents.py                  # run + schedule CRUD
│   ├── trades.py                  # CRUD + CLI order placement
│   ├── exit_signals.py            # GET + acknowledge + recalculate
│   └── risk_settings.py           # GET + PATCH
├── services/
│   ├── agent_runner.py            # asyncio subprocess + SSE streaming
│   ├── polymarket_cli.py          # async wrapper around polymarket clob
│   ├── exit_signal_calculator.py  # edge flip / risk threshold logic
│   └── scheduler.py               # APScheduler integration
└── alembic/                       # DB migrations
```

### Frontend (`frontend/`)
```
frontend/
├── Dockerfile
├── package.json
├── vite.config.js
└── src/
    ├── App.jsx
    ├── api/client.js
    ├── components/
    │   ├── Layout.jsx
    │   ├── StatusBadge.jsx
    │   ├── EdgeBar.jsx
    │   ├── AgentStatusBanner.jsx
    │   └── RiskSettings.jsx
    └── views/
        ├── OpportunitiesView.jsx
        ├── PredictionsView.jsx
        ├── TradesView.jsx
        ├── ExitSignalsView.jsx
        └── AgentControlsView.jsx
```

## Postgres Schema

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type      TEXT NOT NULL CHECK (order_type IN ('limit', 'market')),
    entry_price     NUMERIC(10, 6) NOT NULL,
    size            NUMERIC(18, 6) NOT NULL,
    order_id        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'open', 'filled', 'cancelled', 'failed')),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE predictions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id             TEXT NOT NULL,
    question              TEXT NOT NULL,
    predicted_probability NUMERIC(10, 6) NOT NULL,
    edge                  NUMERIC(10, 6) NOT NULL,
    confidence            TEXT,
    evidence_summary      TEXT,
    graph_id              TEXT,
    market_yes_price      NUMERIC(10, 6),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE exit_signals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id       UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    prediction_id  UUID NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    signal_type    TEXT NOT NULL CHECK (signal_type IN (
                       'edge_flipped', 'edge_below_threshold',
                       'probability_shifted', 'market_moved')),
    message        TEXT NOT NULL,
    acknowledged   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE agent_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_type      TEXT NOT NULL CHECK (agent_type IN ('scanner', 'research')),
    cron_expression TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run        TIMESTAMPTZ,
    next_run        TIMESTAMPTZ,
    last_status     TEXT,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_type)
);

CREATE TABLE risk_settings (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    min_edge                NUMERIC(6,4) NOT NULL DEFAULT 0.05,
    max_position_usdc       NUMERIC(12,2) NOT NULL DEFAULT 100.00,
    prob_shift_threshold    NUMERIC(6,4) NOT NULL DEFAULT 0.10,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO risk_settings (min_edge, max_position_usdc, prob_shift_threshold)
VALUES (0.05, 100.00, 0.10);
```

## API Routes

### Opportunities
- `GET /api/opportunities` — reads opportunities.json

### Predictions
- `GET /api/predictions` — all predictions from DB
- `GET /api/predictions/{market_id}` — latest for a market
- `POST /api/predictions/sync` — upsert results/*.json into DB

### Agents
- `POST /api/agents/scanner/run` — spawn scanner subprocess, return job_id
- `POST /api/agents/research/run` — spawn research subprocess, return job_id
- `GET /api/agents/jobs/{job_id}/stream` — SSE log stream
- `GET /api/agents/status` — current running state
- `GET /api/agents/schedules` — schedule config
- `POST /api/agents/schedules` — upsert cron schedule
- `DELETE /api/agents/schedules/{agent_type}` — disable schedule

### Trades
- `GET /api/trades` — trades from DB
- `POST /api/trades` — place order via CLI + record in DB
- `PATCH /api/trades/{id}` — update status/notes
- `POST /api/trades/{id}/cancel` — cancel via CLI
- `GET /api/trades/orders` — live CLOB orders
- `GET /api/trades/history` — live trade history
- `GET /api/trades/balance` — USDC balance

### Exit Signals
- `GET /api/exit-signals` — unacknowledged signals
- `POST /api/exit-signals/{id}/acknowledge`
- `POST /api/exit-signals/recalculate`

### Risk Settings
- `GET /api/risk-settings`
- `PATCH /api/risk-settings`

## Docker Compose Additions

Add to docker-compose.yml:
- `postgres:16-alpine` — internal only, port 5432
- `backend` — FastAPI on port 8001, bind-mounts ./data and polymarket binary
- `frontend` — nginx serving React build on port 5174, proxies /api/ to backend

## Build Order

1. Postgres + schema (Alembic migration)
2. FastAPI skeleton + health check + GET /api/opportunities
3. Agent runner (subprocess + SSE stream)
4. Predictions sync (read results/*.json → DB)
5. Trade management (polymarket CLI wrapper, read-only first)
6. Trade placement + cancellation
7. Exit signal calculator
8. Scheduler (APScheduler)
9. React scaffold + OpportunitiesView
10. Remaining views (Predictions, Trades, ExitSignals, AgentControls)
11. Production Docker build (nginx)

## Key Decisions

- **No task queue** — asyncio subprocesses are sufficient for 2-agent scheduling. Job state is in-memory; if multi-worker uvicorn is needed later, move to Postgres job table.
- **Predictions sync via file, not DB write** — research_agent.py stays unchanged; backend reads result JSON files and syncs to DB after each run.
- **Polymarket CLI mounted as bind mount** — Rust binary is statically linked, works inside slim Python container.
- **No auth** — single-user local system. Private key passed via env var, never to frontend.
- **confidence column nullable** — not returned by current LLM call; reserved for future enrichment.
- **React not Vue** — independent of MiroFish frontend, no shared components.
