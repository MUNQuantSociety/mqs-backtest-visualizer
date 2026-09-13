# MQS Backtest Visualizer — Backend

Backtest storage now saves **successful reports only**, as one JSONB document
in `app.backtest_reports`. Jobs, progress, and failures remain temporary until
completion. See [completed report storage](docs/COMPLETED_REPORT_STORAGE.md)
for the seven-column schema, API behavior, migration, and restart limits.
This supersedes the database-backed job lifecycle described in older sections.

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

Backtest submission, strategy validation, daily reports, CSV/JSON exports and
local/S3 strategy storage are implemented in this checkout. `/live/*` still
serves generated sample data. This describes repository behavior; it does not
assert that a release is deployed.

For the request/worker/database design, read [Architecture Flow](docs/ARCHITECTURE_FLOW.md).
For current result semantics, read [Report Contract](docs/REPORT_CONTRACT.md).
For portfolio publication, S3 execution and visible terminal logs, read
[S3 strategy flow and logging](docs/S3_STRATEGY_FLOW.md).

For a stable local Windows API, run `./scripts/start-dev-api.ps1`. It launches
the application in your terminal on port 8000; press Ctrl+C to stop it. Only
the explicit `-Background` option writes timestamped output/error files under
`logs/` and waits for `/api/health` before reporting Ready. Stop the existing
process before starting another; restart it after backend code changes. This
avoids an auto-reloader retaining the port when its application child exits.
API console logging runs on bounded background queues, so a stalled terminal
does not block requests. S3 sessions and clients are confined to one thread
and process, then reused there for concurrent catalogue checks.
For completed work, verification evidence and remaining release blockers, read
[Project Status and Handoff](PROJECT_STATUS.md).
[CI and deployment](#ci-and-deployment) below summarizes the current workflow files;
dated architecture/readiness notes remain useful historical context.

---

## Table of contents

- [Quick start](#quick-start)
- [Run a backtest end to end](#run-a-backtest-end-to-end)
- [API endpoints](#api-endpoints)
- [Reports and exports](#reports-and-exports)
- [CI and deployment](#ci-and-deployment)
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
- [Readiness report](docs/READINESS.md) — measured database latency, data horizon, and the ranked gap list

---

## Quick start

Use **Python 3.12** and run commands from the repository root. A reachable
PostgreSQL database with permission to create/write `app.*` is needed to seed
strategies and run the API. For prices, set `FMP_API_KEY` in the backend `.env`:
coverage, indicator warmup, event runs and fast runs then use FMP daily history.
The [isolated tests](#tests) do not need a database. There is no Redis, Celery or
separate worker service to start.

FMP uses the [stable daily OHLCV endpoint](https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-full).
Each run fetches its selected tickers and dates, including lookback history;
the old database parquet cache is not used. Coverage answers are cached for up
to five minutes and end on the latest available prior exchange date. A window
before an IPO or past available history is rejected with the actual bounds.
Provider failures appear as retryable errors, not missing tickers or demo data.
`MARKET_DATA_SOURCE=database` restores database prices and requires readable
`public.market_data`; `MARKET_DATA_SOURCE=fmp` explicitly requires the FMP key.
With no source override, FMP is required. A missing key fails explicitly; it never
switches to database prices. Each run downloads its tickers concurrently (up to
four at a time) and shares that history between indicator warmup, simulation and
benchmark construction.
Daily FMP runs export observed daily results and skip the synthetic minute-by-minute
CSV, which would expand daily closes across nights and weekends.
Restart the API and workers after changing `.env`.

### Windows PowerShell

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Edit the existing `.env` to configure your database before continuing; the
copy step above leaves an existing file untouched. Then seed the catalogue
and start the API in your terminal:

```powershell
.\venv\Scripts\python.exe scripts/seed_strategies.py
.\venv\Scripts\python.exe -X faulthandler -u -m uvicorn server:app --host 127.0.0.1 --port 8000 --log-level info
```

Keep that terminal open; press **Ctrl+C** to stop the API. The helper
`.\scripts\start-dev-api.ps1` runs the same foreground command after checking
that the port is free. Use `-Port 8001` to choose another port. Only an explicit
`-Background` starts a hidden process and redirects output and crash traces to
timestamped files under `logs/`.

### Linux/macOS

```bash
python3.12 -m venv venv
venv/bin/python -m pip install -r requirements.txt
if [ ! -e .env ]; then cp .env.example .env; fi
```

Configure `.env` before running these commands:

```bash
venv/bin/python scripts/seed_strategies.py
venv/bin/python -X faulthandler -u -m uvicorn server:app --host 127.0.0.1 --port 8000 --log-level info
```

Open [API docs](http://localhost:8000/docs) or
[health](http://localhost:8000/api/health). API startup creates missing schema
objects and starts the process pool; seeding upserts built-in catalogue entries.
Neither operation is a general schema migration system. `LOG_LEVEL` controls
application logging; Uvicorn's flag controls its own logger. `logs/` and
`*.log` are gitignored.

The default launch omits `--reload`: its supervisor can retain the port after
the application child exits, leaving HTTP requests unanswered. `faulthandler`
prints Python thread stacks for native crashes. Logs show request arrival/response, S3 package checks
and downloads, selected controls, coverage, queue/worker stages, progress and
report persistence. See the [logging command and stage reference](docs/S3_STRATEGY_FLOW.md#start-the-backend-with-visible-logs).
In S3 mode, seeding alone does not publish strategy files; follow the
[portfolio publication steps](docs/S3_STRATEGY_FLOW.md#publish-the-two-built-in-portfolios).

Keep the pandas/NumPy constraints in [requirements.txt](requirements.txt):
the vendored engine depends on that compatible runtime. Optional cache warming
from an existing MQSMaster checkout is available through
`scripts/seed_market_cache.py --help` (use the virtual-environment interpreter).
The market-data parquet cache lives under `data/backfill_cache/` by default;
a cold cache can make the first run considerably slower.

### Connect the frontend to the real API

Keep the backend running, then open a second terminal. The sibling
`Backtest_Visualiser_FE` checkout requires **Node >=20.19** in its `package.json`.
From the backend repository root, use PowerShell:

```powershell
cd ../Backtest_Visualiser_FE
npm.cmd ci
$env:VITE_USE_FIXTURES = 'false'
$env:VITE_API_BASE_URL = '/api'
$env:DEV_API_PROXY_TARGET = 'http://127.0.0.1:8000'
npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

On Linux/macOS, in that same sibling checkout:

```bash
npm ci
VITE_USE_FIXTURES=false VITE_API_BASE_URL=/api DEV_API_PROXY_TARGET=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

Open **http://127.0.0.1:5173**. Use that exact address: `localhost` may resolve
to a different listener. `--strictPort` fails clearly if the requested port is
occupied instead of silently selecting another one. These process-scoped
settings do not overwrite the frontend's existing environment files. Vite
proxies `/api` to this backend; check
[proxied health](http://127.0.0.1:5173/api/health) before submitting a run.

Use the project's existing login flow and approved access if prompted; do not
put credentials or tokens in `VITE_*` variables, which reach the browser bundle.
A frontend login is not backend authorization or an execution sandbox; the
[security requirements](#security-this-executes-user-supplied-python) still apply.

### Why `--reload` is safe here

The process pool is created in the FastAPI lifespan, not at module import, so
Windows-spawned workers do not recursively create pools. Development reload
can interrupt an in-flight run. On startup, reconciliation fails stale
`running` rows whose worker heartbeat has expired and resubmits unclaimed
`queued` rows; a recent heartbeat protects work owned by another process.

`RUN_HEARTBEAT_INTERVAL_SECONDS` and `RUN_HEARTBEAT_STALE_SECONDS` control that
distinction. See [startup and worker flow](docs/ARCHITECTURE_FLOW.md#2-startup)
for the lifecycle details. Do not run `--reload` as a production process manager.

---

## Run a backtest end to end

The following older worked example is retained as an API usage reference.
Its run IDs, dates, numerical results and catalogue totals are historical,
not current coverage or validation evidence. The curl examples below use Bash
syntax; use the interactive API docs or adapt them with `curl.exe` on Windows. Check
`GET /api/market-data/coverage?strategyKey=portfolio_2` before choosing a
window. Newly generated reports follow the [Report Contract](docs/REPORT_CONTRACT.md),
including additive metadata/availability fields absent from this example.

### 1. Find a strategy

```bash
curl -s http://localhost:8000/api/strategies
```

Historical example, trimmed to one entry (the seeder now includes four built-ins):

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

`id` is what `strategyKey` takes. `parameters` lists the accepted **strategy**
keys in `params`. The API also separates the reserved execution controls below;
other unknown keys produce a 422 naming the key.

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
| `params` | Strategy keys overlay `config.json` and are validated against `param_specs`. Reserved execution controls are separated first; other unknown keys, wrong types and out-of-range values give a 422. |

Execution controls use the existing `params` wire shape. `slippageBps: 5` maps
to engine slippage `0.0005`; `commissionPerShare: 0.005` charges $0.005 per filled
share on each side in event mode, separately from fill-price slippage. These
are the browser form defaults; omitted controls retain legacy zero costs and
explicit zero is respected. Fast mode requires zero per-share commission.
The same `universe` ticker set preserves configured weights; an explicitly
changed set gets equal weights and its own coverage validation. Empty `signals`
and disabled `sentimentGate` are accepted; nonempty signals or enabled sentiment
are rejected. See [Execution controls](docs/REPORT_CONTRACT.md#execution-controls)
for the engine mapping and cost metadata.

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

The numbers above predate the current daily application statistics and populated
benchmark series. Existing persisted runs are not recalculated just by upgrading
the API; rerun a strategy to generate the current report.

Points about this payload that are easy to misread:

- **Ratios, not percentages.** `totalReturn: -0.75` is −75%. So are
  `maxDrawdown`, `volatility` and `winRate` (`0.8` = 80%).
- **`trades` is longer than `totalTrades`.** The table holds one row per
  *lot*, and a position still open when the window ended is a row with
  `exitDate: null`, `exitPrice: null`, `pnl: 0`. `totalTrades`, `winRate` and
  `profitFactor` count only closed round trips. In the run above: 10 rows, 5
  of them closed. That is also why a run can show `winRate: 0.8` and still
  lose money — the losses are sitting in the open lots, marked to market in
  the equity curve.
- **New reports include a benchmark when prices are available.** It holds the
  configured universe at its weights, using first valid in-window closes and
  only observations known at each sample. Missing benchmark values remain
  `null`; inspect `reportMetadata.benchmark` for coverage and entry rules.
- **Open lots have separate marks.** Their realised `trades[].pnl` remains zero;
  `openPositions` supplies `markPrice` and `unrealizedPnl` when a final mark
  exists. Undefined statistics are identified by `metrics.unavailable`.

The engine can also write diagnostic CSVs into `.artifacts/<run_id>/`, including
legacy analytics. These are separate from the [persisted report exports](#reports-and-exports)
and may use different statistical conventions. Artifact files are local to the
worker, gitignored, and removed when their run is deleted.

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
| `POST` | `/api/backtests` | **Postgres + worker pool** | Submit a run. `202` + `BacktestSummary`; `422` with a one-sentence `detail` for anything the student can fix, including a window outside the universe's market-data coverage. |
| `GET` | `/api/backtests` | **Postgres** | Paginated, newest first. Filters: `search`, `status`, `strategyId`, `page`, `pageSize`. |
| `GET` | `/api/backtests/{id}` | **Postgres** | Detail: metrics and availability, daily equity/benchmark, trades, `openPositions`, `reportMetadata`, parameters and progress/error fields. `404` if unknown. |
| `GET` | `/api/backtests/{id}/exports/{filename}` | **Postgres** | `equity.csv`, `trades.csv`, `metrics.csv`, or `report.json`. Completed runs only (`409` otherwise); unknown run/filename is `404`. |
| `DELETE` | `/api/backtests/{id}` | **Postgres** | Delete or cancel — see the table above. `204`, or `404`. |
| `GET` | `/api/strategies` | **Postgres** | Shared catalogue; private report statistics are omitted (0/null). |
| `POST` | `/api/strategies` | **Postgres + store + worker pool** | Upload source. Scans it, stores it, and queues its validation backtest. `201` + `status: "draft"`; `422` for a rejected source; `413` over 256 KB. |
| `GET` | `/api/strategies/template` | *nothing* | Starter source for the editor. Served so the contract it teaches cannot drift from the engine; a test asserts it passes the check below. |
| `POST` | `/api/strategies/check` | *nothing* | Pre-flight: would this source run here? Reads it with `ast`; stores nothing, executes nothing. Always `200` when the check ran, verdict in `ok`/`issues`; `413` over 256 KB. |
| `POST` | `/api/strategies/upload` | **Postgres** + store | `POST /strategies` for a real file: multipart `file` (`.py`, UTF-8, ≤ 256 KB) plus `name`/`description` form fields. Same scan, same store, same validation backtest, same `201` — with `validationRunId` to poll. |
| `POST` | `/api/strategies/upload/check` | *nothing* | `POST /strategies/check` for a file. Same verdict semantics: `200` either way, problems listed by line. |
| `GET` | `/api/strategies/{key}` | **Postgres** | One strategy **including the ones the catalogue hides**. `validationState` is the real lifecycle (`validating` / `active` / `failed_validation`), `validationRunId` the backtest to open for progress or the failure reason. This is how a client watches an upload. `404` if unknown. |
| `GET` | `/api/market-data/validate-tickers` | **FMP** | Authenticated exact symbol recognition for 1–50 comma-separated `tickers`; returns `valid`/`unknown` per ticker and an `unknown` list. |
| `GET` | `/api/market-data/coverage` | **FMP by default** | Historical date bounds for `tickers` or `strategyKey`; no-history is separate from symbol recognition. Explicit legacy database mode remains available for isolated tests. |
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

## Reports and exports

`GET /api/backtests/{id}` and the download endpoints read the same persisted
report. For a completed run:

```bash
curl --fail -o equity.csv http://localhost:8000/api/backtests/RUN_ID/exports/equity.csv
curl --fail -o trades.csv http://localhost:8000/api/backtests/RUN_ID/exports/trades.csv
curl --fail -o metrics.csv http://localhost:8000/api/backtests/RUN_ID/exports/metrics.csv
curl --fail -o report.json http://localhost:8000/api/backtests/RUN_ID/exports/report.json
```

Replace `RUN_ID`; on Windows PowerShell use `curl.exe`. Downloads do not read
a filesystem path supplied by the caller or depend on worker artifact files.

The public curve keeps the last observation per New York date. Total return
uses initial capital; risk metrics use consecutive observed daily closes.
No prior-date baseline, weekend prices or missing benchmark bars are invented.
A same-date capital baseline is retained as metadata after daily downsampling,
with `includedInDailyCurve: false` and no raw engine `curve_index`.

`metrics.unavailable` maps undefined metric names to reasons. Legacy numeric
fields retain a zero compatibility placeholder, which clients should display
as unavailable when the map contains that key. `metrics.csv` exports an empty
value with a reason instead. `report.json` includes the full camelCase detail,
including `reportMetadata` and supplemental `openPositions`.

See [Report Contract](docs/REPORT_CONTRACT.md) for formulas, benchmark coverage,
fee/lot semantics, metadata keys and exact CSV columns.

## CI and deployment

The intended contribution flow is feature branch → PR into `dev` → PR into
`main`. Configure GitHub branch protection/rulesets to require review and CI;
a workflow file alone does not enforce PR-only merges.

- **`dev` is CI-only.** Pushes to `dev` and PRs targeting `dev` or `main`
  run [ci.yml](.github/workflows/ci.yml): Python 3.12 import/layer checks,
  isolated tests, a disposable PostgreSQL pipeline proof, and an image smoke
  build without an AWS push. The aggregate `test` job checks their outcomes.
- **`main` is the deployment path.** A push after merging a PR, or an explicit
  dispatch on `main`, invokes [deploy.yml](.github/workflows/deploy.yml).
  It reruns CI for that commit before assuming the configured OIDC role,
  building/pushing an image, and updating the existing ECS service.
- **Deployment is gated.** It requires the production environment's configuration,
  `AWS_DEPLOY_ROLE_ARN`, and `PRODUCTION_DEPLOY_ENABLED=true`. Missing settings
  or a closed gate fail clearly. The workflow checks secure database TLS,
  preserves task configuration, deploys an image by digest, and verifies the
  intended task revision/digest after ECS becomes stable. It provisions no
  infrastructure and defines no separate development deployment.

[CI/CD operational notes](docs/CI_CD.md) retain older setup/reference material.
Use the current workflow files for actual triggers, required variables and
release gates; the older no-op-deploy and skip-based test descriptions in that
document do not describe these workflows. Nothing here asserts that the
production gate, cloud resources or a release have been verified live.

## The run pipeline, and why it is not synchronous

The API returns `202` with a queued run ID. A process-pool worker claims the
row, executes the existing engine with progress/cancellation callbacks, and
persists metrics, daily observations and FIFO-paired trades before marking the
run terminal. Only the run ID crosses the process boundary; each worker owns
its database connection. Claim predicates prevent duplicate execution.

The request/worker diagrams and layer-by-layer file map live in
[Architecture Flow](docs/ARCHITECTURE_FLOW.md), rather than being duplicated here.
Operational details to keep in mind:

- The pool is created in the FastAPI lifespan, not at import time.
- Progress writes are throttled; cancellation is cooperative between engine
  operations, not an interrupt of a running query or strategy callback.
- Daily persistence keeps the last sample on a date, including the final
  engine mark. Money and ratios use `NUMERIC` columns.
- Shutdown can interrupt runs; heartbeat-based reconciliation handles stale
  running rows and unclaimed queued rows at startup.

The architecture document's dated limitations are historical; use this README
and the report contract for current fast-mode, benchmark and storage behavior.

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

Uploaded source goes through [StrategyStore](src/integrations/strategy_store.py).
`LocalStrategyStore` is the development default; `S3StrategyStore` implements
the same whole-object operations and per-run materialization. Select S3 with
`STRATEGY_STORE_BACKEND=s3` and `STRATEGY_STORE_S3_BUCKET`; optionally set
`STRATEGY_STORE_S3_PREFIX`, `AWS_REGION`, and an explicit local-emulator
`STRATEGY_STORE_S3_ENDPOINT_URL`. Credentials come from the SDK's default
chain (for example, an ECS task role), not hardcoded access keys.

**Stored/staged → validating → active:** source and generated config are stored
under one newly allocated `strategies/<key>/` package before a disabled
`validating` registry row is submitted for validation. Only a successful
validation marks that row `active` and enables it. Failure leaves it disabled.
This is a registry lifecycle: there is no copy/rename into an `active/` S3
prefix. Both stages use the same stored package. Workers materialize it into
a temporary directory before importing it.

A partial write to a fresh package triggers cleanup; an identical completed
package can be retried, and a conflicting existing package is not overwritten.
The configured bucket and task-role permissions must already exist; selecting
S3 does not provision them.

The integration stack's `strategy_bucket_name` output currently identifies
`mqs-backtest-visualizer-strategies-855603407903-us-east-2` in `us-east-2`.
Local S3 development uses `STRATEGY_STORE_S3_PREFIX=development` with an approved
developer AWS profile. Production uses `STRATEGY_STORE_S3_PREFIX=production`:
the task role permits bucket listing for, and object access only under,
`production/strategies/*`. It cannot read development packages. Keep the
registry, chosen prefix and IAM scope aligned; configuring storage does not
deploy the API or grant permissions. Reconfirm the infrastructure output before
an operational cutover.

The local layout below maps to
`s3://<bucket>/<optional-prefix>/strategies/<key>/` for S3:

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

### Migrate existing local packages to S3

This is an opt-in operator cutover, not an isolated test. It reads the configured
application registry and contacts the explicitly named S3 bucket. Review the
database target, `STRATEGY_STORE_ROOT`, AWS identity and destination first;
retain backups of the registry and local packages. The source is always local,
even if `STRATEGY_STORE_BACKEND` already says `s3`.

From the backend repository root, start with the **read-only dry-run** (default):

```powershell
$strategyBucket = 'mqs-backtest-visualizer-strategies-855603407903-us-east-2'
.\venv\Scripts\python.exe scripts/migrate_strategy_store_s3.py --bucket $strategyBucket --prefix development --region us-east-2
```

The example targets local-development storage. For an approved production
cutover, explicitly use `--prefix production` with an identity authorized there;
do not weaken IAM or reuse the development prefix for the production task.

Before applying, stop **all API instances and workers, including validation
workers on other hosts**, and prevent package edits. Keep them stopped through
verification and the coordinated backend switch. The flag below acknowledges
that prerequisite; the script cannot stop or inspect remote workers for you.

```powershell
# Only after all API/worker instances are stopped; keep the reviewed target.
.\venv\Scripts\python.exe scripts/migrate_strategy_store_s3.py --bucket $strategyBucket --prefix development --region us-east-2 --apply --api-workers-stopped
```

On Linux/macOS use `venv/bin/python`, assign `strategyBucket='...'` to the same
reviewed bucket, and pass `--bucket "$strategyBucket"` with the same flags.
The script copies registry-referenced user packages regardless of active/disabled
status, preserving `storage_key` and exact bytes. It excludes built-ins and
unreferenced local files. Conditional writes do not overwrite existing objects;
equal objects are accepted and conflicts fail. Both `strategy.py` and
`config.json` are verified byte-for-byte and with SHA-256. No registry updates,
deletes, bucket provisioning or IAM changes occur.

Only after an **apply exit code of 0** and complete verification, set
`STRATEGY_STORE_BACKEND=s3`, the reviewed bucket, matching prefix and region in
the existing configuration for every API/worker instance, then restart together.
A dry-run exit of 0 is a plan, not a completed copy. On a nonzero exit, do not
switch or restart: keep services stopped, resolve the cause and rerun. Two S3
objects are not an atomic package write, so an interruption may leave one half;
a rerun verifies existing bytes and copies only missing objects. Local originals
remain available for rollback, but reconcile any post-cutover uploads/deletions
before switching back. See the script's `--help` for bounded inventory limits.

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

`context.buy(ticker)` moves toward the configured long `WEIGHTS` allocation,
covering a short first; absent weights use equal allocation. `context.sell(ticker)`
reduces an existing long to flat and does nothing when flat or short.
`context.close_position(ticker)` closes either side. For deliberate signed
exposure, use `context.target_weight(ticker, weight)`: `0.25` targets a 25% long,
`-0.25` targets a 25% short, and `0` targets flat. Each helper accepts `confidence`
to trade that fraction of the remaining adjustment, subject to whole-share
rounding, cash, margin, and execution costs.

---

## Security: this executes user-supplied Python

Validation imports uploaded Python into a worker with the configured database
and storage access. The AST scan restricts imports, dangerous builtins and
interpreter escape attributes; a short data window and cooperative timeout
bound ordinary validation work. These controls are not a sandbox, and a
strategy that never returns to the engine may not observe cancellation.

Authentication/owner enforcement and execution isolation remain separate
requirements before broader exposure. Use least-privilege roles and restrict
access. A future sandbox needs process/container resource limits, controlled
network access and database permissions restricted to the intended scope:

- Read `public.market_data`; the application owns its `app.*` schema.
- Do not touch live-trading tables such as `positions_book`, `cash_equity_book`,
  `pnl_book`, `risk_book`, `portfolio_weights`, `trade_execution_logs`,
  `news_sentiment`, `rbp_forecasts` or `user_creds`.

See the historical [backend design](BACKEND_PLAN.md) for the original scope
and the [source scan](src/services/strategy_validation/scanning.py) for the
actual allowlist. Passing validation means the strategy ran, not that it is
safe to execute without isolation.

---

## Configuration

`.env.example` is the annotated template; use the non-overwriting copy commands
in [Quick start](#quick-start) only if `.env` is absent. Configure the database
and, if selected, the S3 store.

Application settings come from [src/core/config.py](src/core/config.py).
The standalone engine's [database adapter](engine/data/db_adapter.py) is the
intentional independent reader. Both accept `MARKET_DATA_*` aliases for
`HOST`, `PORT`, `DB`, `USER`, `PASSWORD` and `SSLMODE`; a nonempty
`POSTGRES_*` value wins over its alias. Existing process environment values
take precedence over the same names loaded from `.env`.

The knobs most worth knowing:

| Variable | Default | What it controls |
| --- | --- | --- |
| `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD` | Port `25060`, database `mqsdb` | Database connection; equivalent `MARKET_DATA_*` aliases are accepted by API and engine. |
| `POSTGRES_SSLMODE` / `MARKET_DATA_SSLMODE` | `prefer` | Preserve the database operator's explicit TLS policy. `prefer` permits fallback; production deployment requires `require`, `verify-ca`, or `verify-full`. No automatic TLS downgrade on failure. |
| `MAX_CONCURRENT_RUNS` | `2` | Worker pool size. Bounded by cores, not by request volume. |
| `MAX_BACKTEST_WINDOW_DAYS` | `1825` | Largest window `POST /backtests` accepts. |
| `PROGRESS_WRITE_INTERVAL_SECONDS` | `1.0` | Floor between a worker's progress/cancel round trips. |
| `VALIDATION_TIMEOUT_SECONDS` | `600` | Wall-clock backstop on a validation run. |
| `VALIDATION_WINDOW_DAYS` / `_INITIAL_CAPITAL` | `30` / `100000` | The window and capital a validation run uses. |
| `ARTIFACT_DIR` | `.artifacts` | Engine CSVs, one directory per run. |
| `MARKET_CACHE_DIR` | `data/backfill_cache` | Parquet market-data cache, one file per ticker. |
| `STRATEGY_STORE_ROOT` / `_BACKEND` | `.strategy_store` / `local` | Uploaded strategy source; `local` or `s3`. |
| `STRATEGY_STORE_S3_BUCKET` / `_PREFIX` | Empty | Bucket required for S3; optional namespace prefix. |
| `STRATEGY_STORE_S3_ENDPOINT_URL` | Empty | Optional LocalStack/MinIO URL; falls back to `AWS_ENDPOINT_URL`. Leave unset for normal AWS S3. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Empty | Explicit region, otherwise SDK resolution. |
| `LOG_LEVEL` / `MARKET_TIMEZONE` | `INFO` / `America/New_York` | Application logging and fill calendar dates; keep New York for alignment with engine equity/benchmark dates. |

Relative paths resolve against the **repository root**, not the working
directory, because workers and scripts get launched from wherever the operator
happens to be standing. All three storage directories are gitignored and
created on demand.

`.env` is gitignored and stays that way. Never paste a real credential into
`.env.example`, and never log a connection URL — the settings module hands out
SQLAlchemy `URL` objects precisely because their `repr` masks the password.

Protected routes require `Authorization: Bearer <Cognito access token>`. Configure
`AUTH_COGNITO_ISSUER` and `AUTH_COGNITO_CLIENT_ID`; ID tokens are rejected. The
backend checks the configured pool's RS256 signing keys, issuer, client ID,
access-token use, expiry and issued-at claims. Trusted JWKS are cached for five
minutes, with a five-second request timeout and a 30-second rotation cooldown.
Missing or invalid production auth settings prevent startup; this configuration
check does not contact Cognito.

`GET /api/auth/me` returns `{id, email, displayName}`. The ID is an app-owned
UUID mapped uniquely to the verified issuer and opaque subject in `app.users`;
profile fields may be null. Existing report owners are preserved. Legacy
`public.user_creds` passwords are never read, and accounts/reports are not linked
by email. Sign-in creates only the new app identity; it does not claim old reports.

Health and shared strategy metadata remain public. Catalogue report statistics
are always `0`/`null`; use authenticated backtest history for personal activity.
All run creation, history, detail, equity, exports, deletion and strategy uploads
use the verified app UUID. Logout removes browser credentials; already issued
JWTs remain valid until expiry (no per-request token-revocation lookup).

For local tests only, `AUTH_ALLOW_DEV_IDENTITY=true` can enable `X-User-Id` and
`TEMPORARY_USER_ID` when `APP_ENV` is `development` or `test` and both Cognito
settings are blank. It defaults to false and is never honored in production or
staging. A supplied bearer credential never falls back to this testing path.
See [production configuration](docs/PRODUCTION_AUTH.md) for the hosted setup.

---

### Universe validation and execution mode

Before adding a ticker, the run form calls the authenticated
`GET /api/market-data/validate-tickers?tickers=AAPL,MSFT` endpoint. Symbols are
trimmed, uppercased and matched exactly against FMP symbol metadata. A successful
lookup returns `{tickers: [{ticker, status}], unknown: []}`, where status is
`valid` or `unknown`. Unknown means FMP does not recognize that exact symbol;
coverage is a separate check and can still report missing history for a known
symbol. The backend repeats this validation before registering a new job.

Metadata requests use the server's FMP key, at most four concurrent provider
calls, a six-second socket timeout and one bounded transient retry. Successful
recognition is cached for one hour; unknown results for five minutes, up to 512
symbols. Provider outages, plan errors, malformed responses and truncated
searches without an exact match return 503 and are not cached as unknown.

New `POST /api/backtests` requests use `mode: "event"` (also the default).
Explicit `fast` requests are rejected before coverage checks or job creation.
Existing fast-mode reports and standalone engine support remain available.

## The database

The application uses PostgreSQL for market-data reads and its own report/registry
schema. The historical MQS instance below was recorded as 17.6/`mqsdb`; the
current connection is determined by configuration.

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

These ranges are historical, not a current coverage guarantee. Query
`GET /api/market-data/coverage` by strategy or ticker before choosing dates.
`POST /api/backtests` checks the requested window against the universe's
coverage and rejects out-of-range submissions. Boundary coverage does not prove
that every interior bar exists; the engine still fails if a run has no usable
observations. Validation windows are anchored to available data, not today's date.

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

The full [file map](docs/ARCHITECTURE_FLOW.md#8-file-map) and
[layering rules](docs/ARCHITECTURE_FLOW.md#7-layering-rules) live in Architecture Flow.

| Area | Responsibility |
| --- | --- |
| `server.py`, `src/api/`, `src/schemas/` | ASGI entrypoint, HTTP routes and camelCase contracts. |
| `src/services/`, `src/repositories/`, `src/models/` | Reporting/business rules, async database access and `app.*` models. |
| `src/workers/` | Process-pool execution, synchronous persistence and reconciliation. |
| `src/integrations/` | Local/S3 strategy storage; vendor SDKs stay here. |
| `engine/` | Standalone vendored engine, contracts, strategies and analytics. |
| `scripts/`, `tests/` | Operational tools and isolated/explicit database checks. |

Routes delegate to services and repositories. `engine/` does not import
`src/`, FastAPI or SQLAlchemy; its independent database adapter preserves
that boundary. [engine/VENDORED_FROM](engine/VENDORED_FROM) records upstream
provenance. Older `auth/` and `route/` directories are scaffold placeholders.

---

## Operational scripts

Read each script's `--help` before running it. Seeding writes to the configured
application database; the S3 verifier performs an explicit temporary-object
round trip. These are operator commands, not part of isolated tests.

| Script | What it does |
| --- | --- |
| `scripts/seed_strategies.py` | Creates the `app` schema and upserts the four built-in strategies. Idempotent; leaves uploads, run history and `created_at` alone. Run after any schema change and on any fresh database. |
| `scripts/check_market_data.py` | Measures `market_data` coverage for the seeded universes — first and last bar per ticker, and the window that is safe for all of them. `--all-tickers` walks the whole tape and is opt-in for good reason. |
| `scripts/smoke_engine.py` | Proves the vendored engine can reach the real database: builds `portfolio_dummy` through `EngineDBAdapter` and pulls a short window of daily bars. Run it after touching `engine/data/` or `engine/core/utils.py`. |
| `scripts/seed_market_cache.py` | Copies already-backfilled parquet files out of a local MQSMaster checkout into `data/backfill_cache/`. Optional; turns a first run into a cache hit. |
| `scripts/verify_s3_store.py` | Opt-in store verification against an explicitly named bucket (`--bucket` required); writes/reads/deletes a unique temporary key and tests materialization. Supports an emulator endpoint. |
| `scripts/migrate_strategy_store_s3.py` | Read-only dry-run by default; opt-in conditional copy and byte verification of registry-referenced local packages. Apply requires all API/validation workers stopped; see [cutover instructions](#migrate-existing-local-packages-to-s3). |
| `scripts/check_ci_test_report.py` | Checks the disposable-PostgreSQL JUnit report; rejects empty or skipped integration coverage. |

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Every `db` test skips | The database is unreachable — check the `POSTGRES_*` block, and that you are on a network that can reach the host. The skip reason names the failure. |
| TLS connection failures | Verify the server certificate, trust configuration and configured SSL mode with the database operator. Do not weaken TLS to make a connection succeed; production deploy rejects insecure modes. |
| A run fails immediately with a message about no market data | Check current universe coverage and the run's requested window. Query `/api/market-data/coverage` or use `scripts/check_market_data.py`; the historical dates above may be stale. |
| The first run of the day takes minutes at "loading data" | Cold parquet cache against a remote database. Expected once per ticker set; `scripts/seed_market_cache.py` avoids it. |
| Runs stay `queued` forever | The worker pool lives in the lifespan. If the app was constructed without it (e.g. a bare `TestClient(app)` with no `with`), nothing dispatches. |
| A run says "Interrupted by server restart" | Exactly what it says — `--reload` or a deploy killed its worker. The reconciler wrote that message on the next boot. Re-submit. |
| `POST /strategies` returns 422 naming a line | The AST scan refused the source. The message names the line and what would be accepted. |
| `POST /strategies/check` answers `200` for source that is plainly wrong | By design: the check ran, and its answer is in the body. `ok: false` with every problem in `issues`; a status code could carry only one of them. |
| Parquet cache silently never populates | `pyarrow` is missing. It is in `requirements.txt`; the cache layer swallows the failure, so the only symptom is that every run re-queries the database. |

---

## Known limitations and deferred work

> The measured, dated picture — smoke-test numbers, per-ticker data horizon,
> and every gap ranked by what blocks a deploy — lives in
> **[docs/READINESS.md](docs/READINESS.md)**. Regenerate the numbers with
> `venv/Scripts/python.exe scripts/smoke_db.py`.

Honest list of what is not built, so nobody discovers it the hard way.

| Item | Status |
| --- | --- |
| `/live/*` endpoints | Generated sample data. Backing them with the real trading tables is a separate product decision. |
| Authentication | Cognito access tokens map to app-owned users; run reads and mutations enforce owner scope. Shared strategy metadata remains public; report aggregates do not. |
| Real sandboxing for uploaded code | Deferred, and required before this is exposed beyond the club. See [Security](#security-this-executes-user-supplied-python). |
| `mode: "fast"` (the vectorised path) | Standalone engine and historical reports only; new API submissions require `event`. Available for registered adapters, including the built-in `VolMomentum`, `MomentumStrategy` and `RegimeAdaptiveStrategy`; `portfolio_dummy` is unsupported. It is an approximation, retains warmup/first-day-return behavior, and emits no fills. Unsupported classes fail clearly in the engine. |
| Benchmark coverage | Configured-universe buy-and-hold is populated from observed prices. Missing/late entries and stale marks remain possible; inspect metadata. No calendar grid or future-price backfill. |
| Strategy exceptions during validation | The application runs the engine in strict mode: strategy exceptions propagate and fail the run. This is functional validation, not sandboxing. |
| OMS (TWAP/VWAP child-order slicing) | Not vendored — it is live-trading machinery. The engine always takes upstream's documented direct-execution path, so fills differ from an MQSMaster run of the same portfolio. |
| S3 strategy persistence | Implemented behind the same store protocol as local disk; bucket, region, credentials/task role and permissions are deployment prerequisites. |
| Legacy engine analytics | Kept as diagnostic output; they can differ from daily application metrics and extreme short-window CAGR can overflow. API statistics/availability use the report contract. |
| Alembic migrations | Not yet. `create_all` creates missing objects; existing schemas need an explicit migration plan for incompatible changes. |
| SSE / websocket progress | Polling only. Revisit if polling proves insufficient. |
| Portfolios 4–8 as built-ins | They depend on RBP / screener / NLP chains that are not part of this product yet. |

Full historical design rationale and the task-by-task work order:
[BACKEND_PLAN.md](BACKEND_PLAN.md). Current report behavior is described in
[Report Contract](docs/REPORT_CONTRACT.md).


## Tests

Start with the isolated suite. A file being under `tests/unit/` does not imply
that it avoids the database; select by markers explicitly.

Windows PowerShell:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m 'not db and not ci_db'
```

Linux/macOS:

```bash
venv/bin/python -m pytest -q -m 'not db and not ci_db'
```

For a bounded report check, append
`tests/unit/test_report_benchmark.py tests/unit/test_db_adapter.py tests/unit/test_reporting.py`.
Do not use an unfiltered `pytest` invocation as an offline check.

**Disposable PostgreSQL integration is opt-in.** The proof in
[tests/integration/test_ci_pipeline.py](tests/integration/test_ci_pipeline.py)
is marked `ci_db`. It requires `CI_DATABASE_TESTS=1`,
`POSTGRES_HOST=127.0.0.1` (or `localhost`), `POSTGRES_DB=mqs_test`, and
explicit `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
Use only a disposable local instance. It creates fixture market data when the
table is absent, checks existing fixture contents, and writes application rows.
With opt-in enabled, a wrong target or unavailable database fails rather than
silently skipping. The CI workflow supplies its own disposable PostgreSQL
service; it does not use the MQS market-data database.

After configuring that disposable test environment, run:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m ci_db tests/integration/test_ci_pipeline.py
```

On Linux/macOS substitute `venv/bin/python`. Local disposable PostgreSQL may
use `POSTGRES_SSLMODE=disable`; this is not a production TLS recommendation.

**Actual configured-database tests are a separate opt-in operation.** Tests
marked `db` connect to the configured database, read market data and may
create application rows or execute real backtests. Review their fixture dates
and target before choosing individual files. For example, after intentionally
configuring a suitable database:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m 'db and not ci_db' tests/unit/test_run_single.py
```

Again, use `venv/bin/python` on Linux/macOS. The `db` fixture skips if its
connection probe fails; a skipped test is not successful database verification.

When adding integration tests, use `TestClient` as a context manager when the
test needs application lifespan/worker startup. Tests that mock every service
and intentionally avoid lifespan can use the client without starting a pool.
Module/session-scoped live-DB fixtures must depend on `require_database` (or
explicitly check `database_available`) before opening their own connections.
