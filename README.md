# MQS Backtest Visualizer — Backend

A web application that lets MQS members run the society's quantitative
backtests and read the results as charts and tables — without cloning the
trading repo, editing constants in a Python file, or opening a database client.

This repository is the **entire backend**: the HTTP API, the PostgreSQL
persistence layer, the process pool that executes runs, and the backtest engine
itself, vendored out of
[`MQSMaster`](https://github.com/MUNQuantSociety/MQSMaster) and adapted to run
as a service instead of a CLI.

| Repository | Owns |
| --- | --- |
| This one | API, database, job execution, engine |
| Frontend | The entire user interface (React + Vite). Nothing here renders UI. |
| Infrastructure | Terraform and cloud resources. Nothing here provisions. |

> **Status:** the backtest half of the product is real. `POST /api/backtests`
> submits a run, a worker process executes it against `public.market_data`, and
> the results are persisted and served. The `/live/*` endpoints still serve
> generated sample data — see [What is real and what is
> sample](#what-is-real-and-what-is-sample).

---

## Table of contents

- [Quick start](#quick-start)
- [Run a backtest end to end](#run-a-backtest-end-to-end)
- [API endpoints](#api-endpoints)
- [What is real and what is sample](#what-is-real-and-what-is-sample)
- [The run pipeline, and why it is not synchronous](#the-run-pipeline-and-why-it-is-not-synchronous)
- [Uploaded strategies: upload → validate → activate → rerun](#uploaded-strategies-upload--validate--activate--rerun)
- [Security: this executes user-supplied Python](#security-this-executes-user-supplied-python)
- [Configuration](#configuration)
- [The database](#the-database)
- [Repository layout](#repository-layout)
- [Operational scripts](#operational-scripts)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Known limitations and deferred work](#known-limitations-and-deferred-work)

---

## Quick start

A fresh clone plus a filled-in `.env` is a working application. There is no
Redis, no Celery, no separate worker process to start, and no migration step.

```bash
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: venv/bin/python

cp .env.example .env          # then fill in the POSTGRES_* block
venv/Scripts/python.exe scripts/seed_strategies.py
venv/Scripts/python.exe -m uvicorn server:app --reload --port 8000
```

That is the whole install. What each step does:

| Step | Why it is needed |
| --- | --- |
| `pip install -r requirements.txt` | `pandas==2.2.2` and `numpy<=1.26.4` are **hard pins** — the engine's math was validated against exactly those versions. |
| `cp .env.example .env` | Only the `POSTGRES_*` block must be filled in; every other key has a working default baked into `src/core/config.py`. |
| `scripts/seed_strategies.py` | Creates the `app` schema if it is missing and upserts the four built-in strategies. Idempotent — safe to re-run after any schema change. |
| `uvicorn server:app` | Starts the API. The `app` schema and the worker pool are created by the lifespan, so there is nothing else to launch. |

Optional but worth doing on a laptop that also has MQSMaster checked out:

```bash
venv/Scripts/python.exe scripts/seed_market_cache.py   # warm the parquet cache
```

The engine caches market data as one parquet file per ticker under
`data/backfill_cache/`. Without a warm cache the first run over a new ticker
set spends minutes pulling bars from a remote university database; with one it
starts immediately. `seed_market_cache.py` copies the already-backfilled files
out of a local MQSMaster checkout. Reading them requires `pyarrow`, which is in
`requirements.txt` for exactly this reason.

### Why `--reload` is safe here

`--reload` restarts the server on every file save, and this application owns a
`ProcessPoolExecutor`. Those two things are only compatible because **the pool
is built in the FastAPI lifespan and nowhere else** (`src/workers/job_manager.py`).

On Windows a process pool *spawns* its workers, and spawning re-imports the
module tree in each new interpreter. A pool constructed at import time would
therefore be constructed again inside every worker it created, and every one of
those would create its own — under `--reload`, the first file save turns that
into a fork bomb. A lifespan runs exactly once per real server process and
never inside a spawned worker, which makes it the only safe place.

Two consequences you will actually see:

- A reload kills any run in flight. That is not silent data loss: on the next
  boot the reconciler (`src/workers/reconciler.py`) marks runs left `running`
  as `failed` with `"Interrupted by server restart"`, and re-submits runs left
  `queued`, which were never claimed by anything.
- Nothing at import time touches the database or the pool, so `import server`,
  `pytest`, and `python scripts/*.py` are all cheap and side-effect free.

---

## Run a backtest end to end

This is a real transcript, not a sketch. Every value below came back from a
running server against the live database.

### 1. Find a strategy

```bash
curl -s http://localhost:8000/api/strategies
```

Trimmed to one of the three entries:

```json
{
  "items": [
    {
      "id": "portfolio_2",
      "name": "Multi-Indicator Momentum",
      "className": "MomentumStrategy",
      "status": "active",
      "universe": ["AAPL", "TSLA", "AMD", "MSFT", "NVDA"],
      "parameters": [
        {"key": "LOOKBACK_DAYS", "label": "Lookback (days)", "type": "integer",
         "default": 90, "min": 5, "max": 365},
        {"key": "INTERVAL", "label": "Poll interval (seconds)", "type": "integer",
         "default": 60, "min": 60, "max": 86400}
      ],
      "runCount": 0, "bestSharpe": null, "bestReturn": null, "lastRunAt": null
    }
  ],
  "total": 3
}
```

`id` is what `strategyKey` takes. `parameters` is the complete set of keys
`params` accepts — anything else is a 422 naming the key, because an unknown
parameter is a typo, not a feature.

### 2. Submit the run

```bash
curl -s -X POST http://localhost:8000/api/backtests \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Multi-indicator momentum — 2025 to mid-2026",
    "strategyKey": "portfolio_2",
    "startDate": "2025-01-02",
    "endDate": "2026-07-15",
    "initialCapital": 100000,
    "mode": "event",
    "params": {"LOOKBACK_DAYS": 90}
  }'
```

**`202 Accepted`**, with the run row — not the result:

```json
{
  "id": "2347b625-9d9f-47f7-aed0-3f092c469e3c",
  "name": "Multi-indicator momentum — 2025 to mid-2026",
  "strategyId": "portfolio_2",
  "strategyName": "Multi-Indicator Momentum",
  "symbol": "MULTI",
  "timeframe": "1d",
  "status": "queued",
  "startDate": "2025-01-02",
  "endDate": "2026-07-15",
  "createdAt": "2026-08-22T05:05:20.420566Z",
  "initialCapital": 100000.0,
  "finalEquity": 0.0,
  "totalReturn": 0.0,
  "sharpe": 0.0,
  "maxDrawdown": 0.0
}
```

The payload is a complete `BacktestSummary`, so the client can drop it straight
into its list cache. The zeros are deliberate: the frontend's Zod schema types
those four fields as plain numbers, and `status` is what says whether they mean
anything yet.

Request fields:

| Field | Rule |
| --- | --- |
| `name` | Required, ≤ 120 characters. |
| `strategyKey` | Must exist in `app.strategies` and be enabled — otherwise 422. |
| `startDate` / `endDate` | ISO dates, `start < end`, span ≤ `MAX_BACKTEST_WINDOW_DAYS` (1825). |
| `initialCapital` | `> 0`. |
| `mode` | `"event"` (default) or `"fast"`. Only `event` is dependable across every vendored strategy; see [Known limitations](#known-limitations-and-deferred-work). |
| `params` | Overlaid onto the strategy's `config.json` at run time. Validated against `param_specs`: unknown key, wrong type, or out-of-range each give a 422 naming the key. |

Every 422 carries `detail` as a **single sentence string**, not FastAPI's usual
list of error objects, because the frontend's error reader only understands a
string:

```json
{"detail": "'LOOKBAK_DAYS' is not a parameter of strategy 'portfolio_2'. Accepted parameters: INTERVAL, LOOKBACK_DAYS."}
```

### 3. Poll until it finishes

```bash
curl -s http://localhost:8000/api/backtests/2347b625-9d9f-47f7-aed0-3f092c469e3c
```

While it runs, `status` is `queued` then `running` and `progressPct` climbs:

```json
{"status": "running", "progressPct": 89, "errorMessage": null, "...": "..."}
```

`progressPct` and `errorMessage` are on the **detail** response only, never on
list rows.

### 4. Read the results

The same URL once `status` is `completed` — abridged: `equityCurve` shows the
first and last of 308 points, `trades` the first of 10 rows:

```json
{
  "id": "2347b625-9d9f-47f7-aed0-3f092c469e3c",
  "status": "completed",
  "progressPct": 100,
  "errorMessage": null,
  "initialCapital": 100000.0,
  "finalEquity": 24941.2644,
  "totalReturn": -0.750587356,
  "sharpe": -0.5800373276,
  "maxDrawdown": -0.8469964856,
  "metrics": {
    "totalReturn": -0.750587356,
    "cagr": -0.5964373691,
    "sharpe": -0.5800373276,
    "sortino": -0.5320831589,
    "maxDrawdown": -0.8469964856,
    "volatility": 0.6770388212,
    "winRate": 0.8,
    "profitFactor": 75.9385553471,
    "totalTrades": 5
  },
  "equityCurve": [
    {"date": "2025-01-02", "equity": 100000.0, "benchmark": null},
    {"date": "2026-07-15", "equity": 24941.2644, "benchmark": null}
  ],
  "trades": [
    {
      "id": "2347b625-9d9f-47f7-aed0-3f092c469e3c:0",
      "symbol": "AAPL", "side": "long",
      "entryDate": "2025-01-02", "exitDate": "2025-01-03",
      "entryPrice": 243.82, "exitPrice": 243.3,
      "quantity": 82.0, "pnl": -42.64, "returnPct": -0.0021327209, "fees": 0.0
    }
  ],
  "parameters": {"mode": "event", "LOOKBACK_DAYS": 90}
}
```

That run took about 40 seconds of wall clock with a warm cache, produced 308
daily equity points and 10 trade rows.

Three things about this payload that are easy to misread:

- **Ratios, not percentages.** `totalReturn: -0.75` is −75%. So are
  `maxDrawdown`, `volatility` and `winRate` (`0.8` = 80%).
- **`trades` is longer than `totalTrades`.** The table holds one row per
  *lot*, and a position still open when the window ended is a row with
  `exitDate: null`, `exitPrice: null`, `pnl: 0`. `totalTrades`, `winRate` and
  `profitFactor` count only closed round trips. In the run above: 10 rows, 5
  of them closed. That is also why a run can show `winRate: 0.8` and still
  lose money — the losses are sitting in the open lots, marked to market in
  the equity curve.
- **`benchmark` is always `null`.** The engine writes a buy-and-hold benchmark
  to `benchmark_buy_and_hold.csv`, but on a minute grid that does not line up
  with the event-loop samples. Emitting a mismatched series would draw a chart
  that lies, so the field stays null until the engine computes it on the same
  grid.

Alongside the JSON, the engine writes its full CSV report — rolling windows,
monthly returns, correlation matrix, risk decomposition, 14 files — into
`.artifacts/<run_id>/`. That directory is gitignored and is deleted with the
run.

### 5. Delete it, or cancel it

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X DELETE http://localhost:8000/api/backtests/2347b625-9d9f-47f7-aed0-3f092c469e3c
# 204
```

One verb, two behaviours, because that is what the UI's delete button means in
each state:

| Run status | What DELETE does |
| --- | --- |
| `completed` / `failed` | Removes the run, its metrics, equity curve, trades, and `.artifacts/<run_id>/`. |
| `queued` | Same — nothing has claimed it. Deleted under a `status = 'queued'` predicate, so a run claimed in that same instant is cancelled instead of vanishing under its worker. |
| `running` | Cannot be deleted out from under its worker: sets `cancel_requested` and returns immediately. The worker notices within a second and the run lands as `failed` with `errorMessage: "Cancelled by user"`. The row stays; deleting it again takes the terminal path. |

All three answer `204`. An unknown id is `404`.

---

## API endpoints

Everything is mounted under `/api`, which is what the frontend's
`VITE_API_BASE_URL` resolves to. In development Vite proxies that prefix here,
so the browser sees a same-origin URL and CORS is never exercised.

Responses are **camelCase** and the client parses every one of them with Zod. A
renamed or snake_case key is a hard failure in the browser, not a cosmetic
difference — `tests/unit/test_api_contract.py` guards the shapes against the
Pydantic models in `src/schemas/`.

| Method | Path | Backed by | Notes |
| --- | --- | --- | --- |
| `GET` | `/api/health` | — | Liveness. No database, no engine, no I/O. |
| `POST` | `/api/backtests` | **Postgres + worker pool** | Submit a run. `202` + `BacktestSummary`; `422` with a one-sentence `detail` for anything the student can fix. |
| `GET` | `/api/backtests` | **Postgres** | Paginated, newest first. Filters: `search`, `status`, `strategyId`, `page`, `pageSize`. |
| `GET` | `/api/backtests/{id}` | **Postgres** | Detail: metrics, equity curve, trades, `parameters`, plus `progressPct` and `errorMessage`. `404` if unknown. |
| `DELETE` | `/api/backtests/{id}` | **Postgres** | Delete or cancel — see the table above. `204`, or `404`. |
| `GET` | `/api/strategies` | **Postgres** | Catalogue of enabled strategies with SQL-computed run aggregates. |
| `POST` | `/api/strategies` | **Postgres + store + worker pool** | Upload source. Scans it, stores it, and queues its validation backtest. `201` + `status: "draft"`; `422` for a rejected source; `413` over 256 KB. |
| `GET` | `/api/live/portfolios` | *sample data* | Live portfolio list. |
| `GET` | `/api/live/portfolios/{id}` | *sample data* | Detail — config, positions. |
| `GET` | `/api/live/portfolios/{id}/equity` | *sample data* | `days`. |
| `GET` | `/api/live/portfolios/{id}/composition` | *sample data* | `days`. Column-wise series. |
| `GET` | `/api/live/portfolios/{id}/executions` | *sample data* | `ticker`, `page`, `pageSize`. |
| `GET` | `/api/live/portfolios/{id}/correlations` | *sample data* | Full square matrix. |
| `GET` | `/api/live/system/status` | *sample data* | Per-service health. |
| `GET` | `/api/live/system/logs` | *sample data* | `size`. Tail, not archive. |

`GET /api/v1/health` also still answers, kept from the first scaffold commit so
anything already pointing at the versioned path keeps working. New routes go on
`api_router` under `/api`.

Interactive docs at `http://localhost:8000/docs` once the server is running.

### What is real and what is sample

Stating this plainly because the two groups sit side by side in the same
OpenAPI schema and look identical from the outside:

- **The backtest and strategy groups are real.** `/api/backtests*` and
  `/api/strategies*` read and write PostgreSQL. An empty list means there are
  no runs, not that the endpoint is a stub. Results come from the vendored
  engine executing against `public.market_data`.
- **Every `/api/live/*` endpoint is generated sample data** and always has
  been. It is produced by `src/services/sample_data.py` from a fixed seed, so
  it is deterministic — a chart that changed on every refresh would make a
  backend bug indistinguishable from noise. These endpoints describe the
  *live trading system*, which is a different product with its own tables
  (`positions_book`, `cash_equity_book`, `trade_execution_logs`); wiring them
  to those tables is a separate product decision and explicitly out of scope
  here. Until then, nothing behind `/api/live/*` is a real number.

---

## The run pipeline, and why it is not synchronous

An event-mode backtest steps timestamp by timestamp across the requested window
plus a lookback prefix. It is minutes of single-core, GIL-holding Python. There
is no version of this that returns inside an HTTP request:

- **Inline** would block the event loop for the whole run — one student's
  backtest freezes the API for everyone.
- **A thread** would do exactly the same thing, because the work holds the GIL.
- **A process pool** is the smallest thing that works. It gives queueing and a
  responsive API with no extra infrastructure: no broker, no separate worker
  deployment, no second thing to keep alive.

So `POST /backtests` returns `202` with a run id, and the client polls. The
frontend's `useBacktest` hook already refetches while the status is
non-terminal. No SSE, no websockets this phase.

```
POST /api/backtests
      │  validate against the strategy registry
      │  INSERT app.backtest_runs (status='queued')
      │  submit run_id to the pool ─────────────┐
      ▼                                         │
   202 + BacktestSummary                        ▼
                                    ProcessPoolExecutor (max_workers=2)
   GET /api/backtests/{id}                      │  src/workers/run_job.py
      ▲  polls, reads progressPct               │
      │                                         ▼
      │                        UPDATE ... SET status='running'
      │                        WHERE id=%s AND status='queued'   ← the claim
      │                                         │
      │                                         ▼
      │                             engine/run_single.py
      │                             ├─ on_progress(pct, stage)  → throttled UPDATE
      │                             ├─ should_cancel()          → reads cancel_requested
      │                             └─ market data: public.market_data + parquet cache
      │                                         │
      └───── app.run_metrics ◄──────────────────┤  one transaction:
             app.run_equity_points              │  metrics + equity + paired trades,
             app.run_trades                     │  then the run row goes terminal
             app.backtest_runs                  │
                                                └─ CSVs → .artifacts/<run_id>/
```

Details worth knowing before changing any of it:

- **Only the run id crosses the process boundary.** The worker spawns into a
  fresh interpreter that inherited nothing, so it opens its own *synchronous*
  database connection and reads everything else from the run row. The API's
  asyncpg pool does not survive a `fork`/`spawn`; a worker holding one would be
  reading another process's sockets.
- **The claim is the concurrency control.**
  `UPDATE ... WHERE id = ? AND status = 'queued'` affecting zero rows means
  someone else already claimed it, and the job returns. That is what makes
  re-submission (by the reconciler, say) harmless.
- **Progress and cancellation share one throttled round trip.**
  `PROGRESS_WRITE_INTERVAL_SECONDS` (default 1.0) floors the rate; when the
  percentage changed the poll is an `UPDATE ... RETURNING cancel_requested`,
  and when it did not it is a bare `SELECT`. Without a floor a run would spend
  its time talking to Postgres instead of simulating.
- **Cancellation is cooperative.** There is nothing to signal — killing a pool
  worker would leave its run row claimed forever. The engine polls
  `should_cancel()` between timestamp groups, plus once before constructing
  the strategy and once before loading data, so a run cancelled while it is
  still warming up stops in about a second rather than after a multi-minute
  data load.
- **The equity curve is downsampled to daily last-value** before insert. Event
  mode records one sample per poll interval; the charts are daily, and this
  keeps the row count at roughly one per trading day.
- **Money and ratios are `NUMERIC`, not float**, all the way into the table.
- **Shutdown does not wait for running backtests.** Blocking a deploy or a
  Ctrl-C for the ten minutes a long run might have left is worse than losing
  it, and losing it is recoverable — see the reconciler note in
  [Why `--reload` is safe](#why---reload-is-safe-here).

---

## Uploaded strategies: upload → validate → activate → rerun

This is what makes the app a platform rather than a viewer for four fixed
portfolios. A student uploads a `.py` file; the system proves it works **by
running a backtest on it**; if that run passes, the strategy joins the
catalogue and can be re-run like any built-in.

There is no separate drafts table and no second run pipeline. An upload is a
row in `app.strategies` with `kind='user'`, and its whole lifecycle lives in
`status`.

```
POST /api/strategies
   │
   ├─ 1. AST scan            ── violation → 422, nothing stored at all
   ├─ 2. exactly one BasePortfolio subclass? ── no → 422
   ├─ 3. store.put("strategies/<key>/", "strategy.py",  source)
   │     store.put("strategies/<key>/", "config.json", generated)
   ├─ 4. INSERT app.strategies (kind='user', status='validating', enabled=false)
   ├─ 5. INSERT app.backtest_runs (purpose='validation') → the same job pool
   │
   └─ 201 {"status": "draft", "message": "Validation backtest started — ..."}

                    ... the ordinary run pipeline executes it ...

   run completed  → strategy status='active', enabled=true, validation_run_id set
   run failed     → strategy status='failed_validation', enabled stays false
                    (the error is on the run row, which the student can open)
```

| Stage | `app.strategies.status` | Serialised to the client as | In `GET /strategies`? |
| --- | --- | --- | --- |
| Just uploaded | `validating` | `draft` | No |
| Validation passed | `active` | `active` | Yes |
| Validation failed | `failed_validation` | `draft` | No |
| Retired | `archived` | `archived` | No |

The client's Zod enum knows only `active | draft | archived`, so `validating`
and `failed_validation` both serialise as `draft`; the submission `message` is
what distinguishes them. The mapping lives in exactly one place,
`src/services/strategies._STATUS_TO_CLIENT`.

**Rerunning needs no new code.** Once the strategy is `active`, `POST
/backtests` with its key works exactly like a built-in — the only branch is
where the class comes from.

### The validation run

- **Window:** the last `VALIDATION_WINDOW_DAYS` (30) of data, anchored on the
  last bar the universe actually has — never on `now()`. Market data ends weeks
  behind the calendar, so a window computed from today returns zero rows and
  would fail every upload for a reason that has nothing to do with the
  uploaded code.
- **Capital:** `VALIDATION_INITIAL_CAPITAL` (100,000).
- **Timeout:** `VALIDATION_TIMEOUT_SECONDS` (600), enforced through the
  ordinary cancellation flag by a watchdog in the API process. It is a
  backstop, not a resource limit: it dies with the API process, and a restart
  leaves the run to finish on its own with the reconciler cleaning up.
- **Config:** the frontend's upload form sends only name, description, source
  and filename, so the config is generated —
  `TICKERS: ["AAPL", "MSFT"]`, equal `WEIGHTS`, `INTERVAL: 60`,
  `LOOKBACK_DAYS: 30`, `DATA_FEEDS: ["MARKET_DATA"]`. An upload advertises one
  tunable parameter, `LOOKBACK_DAYS`.

### The store layout

Uploaded source goes through one interface, `src/integrations/strategy_store.py`,
written in **S3 vocabulary from day one** — opaque keys, whole-object
put/get/delete, no seeking, no partial writes. The bucket does not exist yet,
so `LocalStrategyStore` backs it with disk today; when infrastructure
provisions one, `S3StrategyStore` is a new class behind the same Protocol, not
a refactor of the callers. (It exists already as a stub whose every method
raises `NotImplementedError("S3 backend arrives with infrastructure")`.
`STRATEGY_STORE_BACKEND=local|s3` selects; do not select `s3`.)

```
.strategy_store/                          ← STRATEGY_STORE_ROOT, gitignored
└── strategies/                           ← the "strategies/<key>/" key prefix
    └── my-strategy-a1b2c3d4/             ← slugified name + short uuid
        ├── strategy.py
        └── config.json
```

That layout is load-bearing, not cosmetic. It is byte-for-byte the shape of
`engine/strategies/portfolio_1/`, because the engine's `BasePortfolio` finds
its `config.json` by looking next to the file its class was defined in
(`inspect.getfile` sibling lookup). `store.materialize(key, dest)` writes a
key's objects into a temp directory in exactly that shape, so a materialized
upload loads through the unmodified engine.

Worker side, per run: materialize into a per-run temp dir, import
`strategy.py` via `importlib.util.spec_from_file_location`, register it in
`sys.modules` under a synthetic per-run name, find the `BasePortfolio`
subclass, hand it to `run_single` like any built-in, delete the temp dir.

### What an uploadable strategy looks like

```python
"""Minimal uploadable strategy — buys once, sells once."""

import logging

from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class MyStrategy(BasePortfolio):
    def __init__(self, db_connector, executor, debug=False, config_dict=None,
                 backtest_start_date=None, order_manager=None):
        super().__init__(db_connector, executor, debug, config_dict,
                         backtest_start_date, order_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._steps = 0

    def OnData(self, context):
        self._steps += 1
        ticker = self.tickers[0]
        if self._steps == 2:
            context.buy(ticker, confidence=1.0)
        elif self._steps == 6:
            context.sell(ticker, confidence=1.0)
```

Exactly one `BasePortfolio` subclass per file — zero means the file is not a
strategy, and two means the answer depends on which one the loader happens to
find first. Both are a 422.

---

## Security: this executes user-supplied Python

Stated plainly, because it is the largest risk in this codebase and softening
it would be dishonest:

**Validating an uploaded strategy means importing and executing untrusted
Python inside a worker process that holds admin credentials to the production
MQS trading database.** That is a deliberate product decision — functional
first, for a small authenticated club — and it is the only reason the
guardrails below are considered sufficient.

The guardrails are:

1. **An AST scan** (`src/services/strategy_validation.scan_source`) that
   refuses imports outside a small allowlist (`engine`, `pandas`, `numpy`,
   `math`, `datetime`, `typing`, `collections`, `statistics`, `logging`),
   refuses `exec` / `eval` / `compile` / `__import__` / `open` / `input` /
   `breakpoint` anywhere they appear, refuses attribute access into `os`,
   `sys`, `subprocess`, `socket`, `pathlib` and friends, and refuses the usual
   routes back into the interpreter (`__globals__`, `__subclasses__`,
   `__code__`, …).
2. **A wall-clock timeout** on validation runs, enforced through the ordinary
   cancellation flag.
3. **A short validation window**, so a validation run is a minute of CPU
   rather than an hour of it.

**None of these is a security boundary.** The scan reads source the interpreter
is about to execute anyway, and any author who wants past it can get past it
with a string, a dunder, or a decorator — it is a speed bump against accidents
and casual mischief, nothing more. The timeout is cooperative: it sets a flag
the engine polls, and code that never returns to the engine loop never sees it.

Real isolation is deferred work and is **required before this application is
exposed beyond the club**: a container per run, no network egress, and a
database role scoped to `SELECT` on `public.market_data` instead of the admin
credentials the worker holds today.

Two related rules that are enforced by code review and nothing else, because
the credentials in `.env` are admin-level:

- The app may **read** `public.market_data` and **owns** everything under the
  `app` schema.
- It must never read or write `positions_book`, `cash_equity_book`, `pnl_book`,
  `risk_book`, `portfolio_weights`, `trade_execution_logs`, `news_sentiment`,
  `rbp_forecasts`, or `user_creds`. This platform simulates; it does not trade.

---

## Configuration

`.env.example` is the annotated template. Copy it and fill in the `POSTGRES_*`
block; every other key has a working default.

```bash
cp .env.example .env
```

**Nothing reads `os.environ` outside `src/core/config.py`.** Importing
`settings` is the only supported way to get configuration, so a typo fails at
startup rather than three screens deep in a worker at 2 a.m.

The knobs most worth knowing:

| Variable | Default | What it controls |
| --- | --- | --- |
| `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD` | — | The MQS instance. The only block you must fill in. |
| `POSTGRES_SSLMODE` | `prefer` | **`require` is rejected by this server**; `prefer` connects and still encrypts. Verified — do not "harden" without retesting. |
| `MAX_CONCURRENT_RUNS` | `2` | Worker pool size. Bounded by cores, not by request volume. |
| `MAX_BACKTEST_WINDOW_DAYS` | `1825` | Largest window `POST /backtests` accepts. |
| `PROGRESS_WRITE_INTERVAL_SECONDS` | `1.0` | Floor between a worker's progress/cancel round trips. |
| `VALIDATION_TIMEOUT_SECONDS` | `600` | Wall-clock backstop on a validation run. |
| `VALIDATION_WINDOW_DAYS` / `_INITIAL_CAPITAL` | `30` / `100000` | The window and capital a validation run uses. |
| `ARTIFACT_DIR` | `.artifacts` | Engine CSVs, one directory per run. |
| `MARKET_CACHE_DIR` | `data/backfill_cache` | Parquet market-data cache, one file per ticker. |
| `STRATEGY_STORE_ROOT` / `_BACKEND` | `.strategy_store` / `local` | Uploaded strategy source. |

Relative paths resolve against the **repository root**, not the working
directory, because workers and scripts get launched from wherever the operator
happens to be standing. All three storage directories are gitignored and
created on demand.

`.env` is gitignored and stays that way. Never paste a real credential into
`.env.example`, and never log a connection URL — the settings module hands out
SQLAlchemy `URL` objects precisely because their `repr` masks the password.

The `SUPABASE_*` / `JWT_*` keys at the bottom of `.env.example` are commented
out and read by nothing. Authentication is a parallel work stream; this repo
only reserves the seams (an `owner_id` column and a `for_user()` filter in the
repositories that currently no-ops).

---

## The database

One PostgreSQL instance (17.6, `mqsdb`), used for two different things.

### `public.market_data` — read-only

The bars the engine simulates over. Order of a billion rows on a remote host,
so every query this repo writes against it is index-shaped by necessity.

Coverage, measured 2026-08-21 (`scripts/check_market_data.py`):

| | |
| --- | --- |
| Tickers in the seeded strategy universes | 14, all present |
| Earliest bar | 2019-11-11 (AAPL, AMD, AMZN, MSFT, NVDA, TLT, TSLA) |
| Latest bar | **2026-07-15** |
| Window safe for all 14 seeded tickers | **2020-01-02 → 2025-11-07** (`TLT` binds the recent end) |
| Distinct tickers in the whole table | Not exactly measurable in a sane budget; **≥ 5,000**, realistically tens of thousands |

Per-ticker ranges: `AAPL`, `AMD`, `AMZN`, `MSFT`, `NVDA`, `TSLA`
2019-11-11 → 2026-07-15 · `WMT` 2020-01-02 → 2026-07-15 · `JPM`
2020-01-02 → 2026-06-22 · `CAT` → 2026-05-26 · `UNH`, `XOM` → 2026-05-21 ·
`GLD`, `^VIX` → 2026-05-20 · `TLT` 2019-11-11 → 2025-11-07.

**The thing to internalise: coverage ends weeks behind today's date.** A window
computed from `now()` returns zero rows, and a run over it fails loudly rather
than returning an empty success. `POST /backtests` does *not* pre-check the
window against coverage — it accepts the dates and the engine reports the empty
range on the run row. Pick dates inside the table.

Note also that `open_price` / `high_price` / `low_price` are NULL for the more
recent bars; only `close_price` and `volume` are populated, which is why the
engine trades on close.

### The `app` schema — owned by this application

Created on startup by the lifespan (and by `scripts/seed_strategies.py`); no
Alembic yet, because the schema is young and nobody outside this repo depends
on it. `Base.metadata.create_all` is what builds it.

| Table | Holds |
| --- | --- |
| `app.strategies` | The registry — built-ins and uploads. `key` PK, `kind`, `status`, `enabled`, `universe`, `param_specs`, `class_path` (built-ins) or `storage_key` (uploads), `validation_run_id`. |
| `app.backtest_runs` | One row per run: `id` UUID PK, `strategy_key`, `status`, `params`, window, `initial_capital`, denormalised `final_equity`/`total_return`/`sharpe`/`max_drawdown`, `progress_pct`, `error_message`, `engine_version`, `cancel_requested`, `purpose` (`user`\|`validation`), `owner_id` (reserved for auth). |
| `app.run_metrics` | The headline numbers, one row per run. Column names are the frontend's `PerformanceMetrics` fields in snake_case. |
| `app.run_equity_points` | `(run_id, seq)` — the daily equity curve. |
| `app.run_trades` | `(run_id, seq)` — round trips from FIFO-paired engine fills. |

The list endpoint reads only the run row, never the metrics table — which is
why the headline numbers are denormalised onto it.

`src/repositories/` is the only place SQL lives for the API (async,
SQLAlchemy 2.0). Worker SQL is synchronous and lives in `src/workers/`; the two
never share a connection.

---

## Repository layout

Code is placed by role. A file that needs two roles is two files.

```
mqs-backtest-visualizer/
│
├── server.py                  ASGI entrypoint. Builds the app, mounts CORS and
│                              the router, and attaches the composed lifespan
│                              (schema first, then the worker pool).
│
├── src/
│   ├── api/routes/            HTTP only: parse, delegate, serialize.
│   │                          Never imports SQLAlchemy or engine.*
│   ├── schemas/               Pydantic request/response models — the frontend
│   │                          contract. camelCase via CamelModel; mirrors the
│   │                          client's Zod types.
│   ├── services/              Business logic. No SQL strings.
│   │                          backtests · strategies · strategy_validation ·
│   │                          trade_pairing · sample_data (/live/* only)
│   ├── repositories/          All async database access. The owner-scoping
│   │                          seam (for_user) lives here.
│   ├── models/                SQLAlchemy ORM models for the app schema.
│   ├── db/                    Engine/session plumbing and schema init.
│   ├── workers/               Job execution. Sync DB only; may import engine.*
│   │                          job_manager · run_job · reconciler
│   ├── integrations/          Adapters to external systems (the strategy
│   │                          store). Vendor SDK types stop here.
│   └── core/                  Settings. The only module that reads the
│                              environment.
│
├── engine/                    The vendored backtest engine. Zero FastAPI,
│   │                          SQLAlchemy or src.* imports — it must stay
│   │                          runnable standalone.
│   ├── contracts/             RunRequest / RunResult / RunCancelled /
│   │                          NoMarketData — the seam the worker calls.
│   ├── run_single.py          run_single(request) -> RunResult. One portfolio,
│   │                          one window, structured data back.
│   ├── core/                  Simulation kernel: engine, event-loop runner,
│   │                          executor, cost model, market-data query.
│   ├── analytics/             Reporting, metrics, vectorised/fast mode, CSCV.
│   ├── strategies/            portfolio_BASE + the four vendored portfolios,
│   │                          each a folder of strategy.py + config.json.
│   ├── indicators/            Technical indicators, loaded by name.
│   ├── data/                  db_adapter.py (the only DB seam) and the
│   │                          parquet cache.
│   └── VENDORED_FROM          Upstream SHA, full copy map, and what was
│                              deliberately not copied.
│
├── scripts/                   Operational one-offs. Import from src/engine,
│                              never duplicate logic.
├── tests/unit/ · tests/integration/
├── .env.example · requirements.txt · pytest.ini
└── BACKEND_PLAN.md            The work order this backend was built from.
```

Two structural rules that are enforced, not aspirational:

- **`engine/` imports nothing from `src/`.** Its only database access is
  `engine/data/db_adapter.py`, which reproduces the exact
  `{"status": ..., "data": [dict-rows]}` contract MQSMaster's connector
  returned. That is what keeps the engine testable without an API and
  swappable later.
- **Routes → services → repositories → database.** A route that imports
  SQLAlchemy is a bug.

Every local modification to vendored engine code carries a `# VISUALIZER:`
comment (33 of them across 7 files), so an upstream diff stays readable.
`engine/VENDORED_FROM` records the SHA it came from.

### Legacy placeholders

`auth/` and `route/` predate this layout and are empty. Remove when convenient.

---

## Operational scripts

All are safe to re-run and take `--help`.

| Script | What it does |
| --- | --- |
| `scripts/seed_strategies.py` | Creates the `app` schema and upserts the four built-in strategies. Idempotent; leaves uploads, run history and `created_at` alone. Run after any schema change and on any fresh database. |
| `scripts/check_market_data.py` | Measures `market_data` coverage for the seeded universes — first and last bar per ticker, and the window that is safe for all of them. `--all-tickers` walks the whole tape and is opt-in for good reason. |
| `scripts/smoke_engine.py` | Proves the vendored engine can reach the real database: builds `portfolio_dummy` through `EngineDBAdapter` and pulls a short window of daily bars. Run it after touching `engine/data/` or `engine/core/utils.py`. |
| `scripts/seed_market_cache.py` | Copies already-backfilled parquet files out of a local MQSMaster checkout into `data/backfill_cache/`. Optional; turns a first run into a cache hit. |

---

## Tests

```bash
venv/Scripts/python.exe -m pytest -q          # everything: 212 passing
venv/Scripts/python.exe -m pytest -q tests/unit    # no database needed
```

Anything that needs the live database carries `@pytest.mark.db`. A session
fixture in `tests/conftest.py` attempts a 3-second connect once; if it fails,
every `db`-marked test **skips with a clear reason** instead of failing, so the
suite is green on a laptop off the university network.

Two traps that have already cost time here:

- **`TestClient` must be used as a context manager.** Outside a `with` block,
  Starlette spins a fresh event loop per request, and the asyncpg pool's
  connections belong to the loop that opened them — the second DB-backed
  request raises `Event loop is closed`. Entering the block also runs the
  lifespan, which is what creates the worker pool at all.
- **A module- or session-scoped fixture is set up *before* the function-scoped
  `db`-marker skip.** A module-scoped DB fixture must therefore request
  `database_available` and `pytest.skip` itself, or an offline machine gets a
  connection error instead of a skip — and an online one can burn ten minutes
  on a backtest before reporting "skipped".

The integration tests submit real backtests against real market data over a
window pinned inside verified coverage (2026-03-02 → 2026-07-15). They are the
end-to-end proof that the pipeline works; they are also why the suite takes
around a minute rather than a second.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Every `db` test skips | The database is unreachable — check the `POSTGRES_*` block, and that you are on a network that can reach the host. The skip reason names the failure. |
| `sslmode=require` connection failures | This server rejects `require`. Use `prefer` (the default). |
| A run fails immediately with a message about no market data | The window is outside coverage. Data ends **2026-07-15**; a window computed from today returns nothing. Run `scripts/check_market_data.py`. |
| The first run of the day takes minutes at "loading data" | Cold parquet cache against a remote database. Expected once per ticker set; `scripts/seed_market_cache.py` avoids it. |
| Runs stay `queued` forever | The worker pool lives in the lifespan. If the app was constructed without it (e.g. a bare `TestClient(app)` with no `with`), nothing dispatches. |
| A run says "Interrupted by server restart" | Exactly what it says — `--reload` or a deploy killed its worker. The reconciler wrote that message on the next boot. Re-submit. |
| `POST /strategies` returns 422 naming a line | The AST scan refused the source. The message names the line and what would be accepted. |
| Parquet cache silently never populates | `pyarrow` is missing. It is in `requirements.txt`; the cache layer swallows the failure, so the only symptom is that every run re-queries the database. |

---

## Known limitations and deferred work

Honest list of what is not built, so nobody discovers it the hard way.

| Item | Status |
| --- | --- |
| `/live/*` endpoints | Generated sample data. Backing them with the real trading tables is a separate product decision. |
| Authentication | Out of scope here. The seams exist (`owner_id` column, a `for_user()` repository filter that no-ops); a parallel session builds Supabase OAuth. Until it lands, **every run is visible to everyone**. |
| Real sandboxing for uploaded code | Deferred, and required before this is exposed beyond the club. See [Security](#security-this-executes-user-supplied-python). |
| `mode: "fast"` (the vectorised path) | Accepted by the API but not implemented by every strategy, and `portfolio_dummy` cannot use it at all. An unsupported combination fails the run with a message naming the strategy rather than being rejected at submit time — checking properly would mean importing the strategy class inside the request. Use `event`. |
| Benchmark series on the equity curve | Always `null`. The engine computes buy-and-hold on a minute grid that does not align with event-loop samples. |
| A strategy that raises inside `OnData` still passes validation | The engine's event loop catches per-timestamp strategy exceptions, logs them and continues, so the run completes. Import-time and construction-time failures *are* caught. Fixing this means having the runner re-raise when `strict` is set. |
| OMS (TWAP/VWAP child-order slicing) | Not vendored — it is live-trading machinery. The engine always takes upstream's documented direct-execution path, so fills differ from an MQSMaster run of the same portfolio. |
| Real `S3StrategyStore` | Stub. Arrives when the infrastructure repo provisions a bucket; the swap is one class behind the existing Protocol. |
| Alembic migrations | Not yet. `create_all` builds the schema; add migrations at the first change after other people depend on `app.*`. |
| SSE / websocket progress | Polling only. Revisit if polling proves insufficient. |
| Portfolios 4–8 as built-ins | They depend on RBP / screener / NLP chains that are not part of this product yet. |

Full design rationale, the task-by-task work order this backend was built from,
and the verified database facts behind it: `BACKEND_PLAN.md`.
