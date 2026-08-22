# Backend Build Plan — Backtest Visualizer

**This document is the work order for building the backend.** It is written to
be executed task-by-task by a model (or human) with no other context. Read
section 0 before writing any code. Each task states its goal, why it exists in
the bigger picture, the exact files it touches, and a concrete acceptance check.

---

## 0. Ground rules for the implementing model

These exist to prevent guessing. Violating any of them produces code that
looks right and fails in the browser or against the production database.

1. **The frontend contract is law, and it lives in another repo.** Before
   creating or changing any response field, open the matching Zod schema in
   `C:/Users/user/OneDrive/Desktop/Backtest_Visualiser_FE/src/features/<feature>/types.ts`
   and copy the field names exactly. Every response key is **camelCase**. The
   FE parses every payload; an invented or renamed key is a runtime failure in
   the browser, not a style issue. Do not invent fields the FE does not read.
2. **`tests/unit/test_api_contract.py` must pass after every task.** Run
   `venv/Scripts/python.exe -m pytest -q`. If a task legitimately changes
   behavior (e.g. list becomes DB-backed and empty), adjust the test in the
   same commit and say so in the commit message.
3. **The database is the production MQS trading database.** Verified facts are
   in section 2 — trust them over any assumption. The app may **read**
   `public.market_data` and **own** everything under the `app` schema. It must
   never read or write `positions_book`, `cash_equity_book`, `pnl_book`,
   `risk_book`, `portfolio_weights`, `trade_execution_logs`, `news_sentiment`,
   `rbp_forecasts`, or `user_creds`. The credentials in `.env` are admin-level,
   so nothing enforces this except this rule.
4. **Never print, log, or commit credentials.** `.env` is gitignored; keep it
   that way. Config is read only via `src/core/config.py`.
5. **When reality disagrees with this plan, reality wins** — then update this
   file in the same commit so the next session inherits the correction.
6. **MQSMaster is read-only reference material.** Copy from
   `C:/Users/user/OneDrive/Desktop/MQSMaster`, never modify it, never import
   from it at runtime. Everything the engine needs gets vendored into this
   repo (task 3).
7. **Do not touch the `/live/*` routes** (`portfolios.py`, `system.py`) or
   their sample data — they describe the live trading system, which is out of
   scope. They keep serving generated sample data.
8. **Auth is out of scope.** A parallel session builds Supabase OAuth.
   Leave the seams described in task 2 (`owner_id` column, `for_user()`
   repository filter) and add nothing else — no JWT parsing, no login routes.
9. **Commit after each task** to `dev`, message format in the task. Do not
   batch tasks into one commit.
10. **Engine version pins are hard requirements:** `pandas==2.2.2`,
    `numpy<=1.26.4`. The engine's math was validated against these; do not
    upgrade them.
11. **Folder structure is fixed — place code by role, not convenience:**

    | Location | Role | Hard rule |
    |---|---|---|
    | `src/api/routes/` | HTTP only: parse, authorize (later), delegate, serialize | Never imports SQLAlchemy or `engine.*` |
    | `src/schemas/` | Pydantic request/response models (the FE contract) | camelCase serialization via `CamelModel`; mirrors FE Zod types |
    | `src/services/` | Business logic | No SQL strings; talks to repositories and integrations |
    | `src/repositories/` | All database access | Only place SQLAlchemy queries live; owner-scoping seam lives here |
    | `src/models/` | SQLAlchemy ORM models (`app` schema) | No behavior beyond columns/relationships |
    | `src/db/` | Engine/session plumbing, schema init | — |
    | `src/workers/` | Job execution processes | Sync DB only; may import `engine.*` |
    | `src/integrations/` | Adapters to external systems (strategy store/S3) | Vendor SDK types never leak past this layer |
    | `src/core/` | Settings, cross-cutting infra | Only module reading the environment |
    | `engine/` | The vendored backtest engine | Zero FastAPI/SQLAlchemy/`src.*` imports — engine must stay runnable standalone; DB access only through its `engine/data/db_adapter.py` seam |
    | `tests/unit` / `tests/integration` | Mirror the package they test | `db` marker on anything needing the live database |
    | `scripts/` | Operational one-offs | Import from `src`/`engine`, never duplicate logic |

    New files go where the table says. A file that needs two roles is two
    files.

### The bigger picture (why any of this exists)

MQS (a student quant society) has a trading system, `MQSMaster`, containing a
proven backtest engine — but running it requires cloning that repo, editing
constants in `main_backtest.py`, and reading CSVs off disk. This project turns
that into a full-stack web app for students: pick a strategy, set dates and
capital, click **Run Backtest**, watch progress, explore the results as charts
and tables. A React frontend (separate repo, separate session) renders it; this
repo is the entire backend: HTTP API, database persistence, and the engine
itself, adapted to run as a service instead of a CLI.

The centerpiece is **`POST /backtests`** — the Run Backtest endpoint. Every
task below either builds toward it, persists its output, or exposes its
results. When in doubt about scope, ask: does this help a student submit a run
and see its results? If no, it is not this session's work.

The second pillar is **user-uploaded strategies** (tasks 8–9). A student
uploads a `.py` strategy; the system proves it compatible **by running a
backtest on it**; if that validation run succeeds, the source is saved to the
strategy store (an S3-shaped interface — local disk now, real S3 bucket later
with zero call-site changes) and the strategy appears in the catalogue. Next
time, the student just selects it: the store fetches the source and the same
run pipeline executes it. Built-ins and uploads flow through one pipeline —
the only difference is where the class is loaded from. (A sample
"expected .py" template shown in the UI comes after this works; deferred
task, noted in section 7.)

---

## 1. Current state (verified 2026-08-21)

| Piece | State |
|---|---|
| 13 routes under `/api` (`src/api/routes/`) | Built, all serving deterministic **sample data** from `src/services/sample_data.py` |
| Pydantic schemas mirroring FE Zod types (`src/schemas/`) | Built and correct |
| Contract tests | 18 passing (`tests/unit/test_api_contract.py`) |
| DB connectivity | **Verified working** (section 2) |
| Engine | Not yet vendored; lives in MQSMaster |
| `POST /backtests` (Run Backtest) | **Does not exist yet — the point of this plan** |
| Auth | Out of scope (parallel session, Supabase OAuth) |

Endpoints the FE calls today (verified by grep over its `*-api.ts`; the FE has
no run-submission call yet — it will be built against task 7's endpoint):

```
GET    /backtests                 GET /live/portfolios
GET    /backtests/{id}            GET /live/portfolios/{id}
DELETE /backtests/{id}            GET /live/portfolios/{id}/equity
GET    /strategies                GET /live/portfolios/{id}/composition
POST   /strategies                GET /live/portfolios/{id}/executions
GET    /live/system/status        GET /live/portfolios/{id}/correlations
GET    /live/system/logs
```

---

## 2. Verified database facts (do not re-derive; connection already proven)

Connected 2026-08-21 with the credentials now in `.env` (`POSTGRES_*` keys,
`sslmode=prefer` — the server **rejects** `require`):

- PostgreSQL **17.6**, database `mqsdb`, schemas `public` and `cair`.
- `public` tables: `market_data`, `cash_equity_book`, `news_sentiment`,
  `pnl_book`, `portfolio_weights`, `positions_book`, `rbp_forecasts`,
  `risk_book`, `trade_execution_logs`, `user_creds`.
- `market_data` columns (from MQSMaster `schemaDefinitions.py`, confirmed
  live): `id serial PK · ticker varchar(10) · timestamp timestamptz ·
  date date · exchange varchar(50) · open_price numeric · high_price numeric ·
  low_price numeric · close_price numeric · volume bigint ·
  avg_sentiment numeric · created_at`.
- The engine's historical query (`MQSMaster/src/backtest/utils.py`) filters to
  NY trading hours (`09:30–16:00 America/New_York`) and aggregates to daily
  bars. All engine timestamps are `America/New_York`.
- The engine reaches the DB through **one interface**:
  `portfolio.db.execute_query(sql, params, fetch=True)` returning
  `{"status": "success"|"error", "data": [dict, ...]}` (dict rows). That is
  the only surface our adapter must reproduce (task 3).
- Coverage caveat: ticker/date coverage of `market_data` was still being
  measured when this was written. **Task 1 includes measuring it** and
  recording the result in this file. The engine's parquet cache layer
  (`backfill_cache/cache.py`) already handles partially-missing DB data by
  querying only missing ranges; a run over a window with no data must fail
  loudly (task 4), not return an empty success.

The app's own tables live in a new **`app` schema** in this same database —
one instance, clean namespace separation. This mirrors the platform plan in
`MQSMaster/docs/platform/BACKTEST_PLATFORM_PLAN.md` (same-instance,
grants-not-servers).

---

## 3. Target architecture

```
                 POST /backtests  (Run Backtest)
Frontend ──────────────────────────► FastAPI (src/api)
   │                                    │ validate params against strategy
   │ poll GET /backtests/{id}           │ registry → insert run row
   │ until status is terminal           │ (status=queued) → submit job → 202
   │                                    ▼
   │                              Job manager (src/workers/job_manager.py)
   │                              ProcessPoolExecutor, max_workers=2
   │                                    │ claim run (queued→running),
   │                                    │ progress writes ~1/sec
   │                                    ▼
   │                              engine/  (vendored from MQSMaster)
   │                              run_single() → BacktestRunner event loop
   │                                    │ market data: public.market_data
   │                                    │ via db adapter + parquet cache
   │                                    ▼
   └── reads results ◄──────────  PostgreSQL  (one instance)
                                    public.market_data   ← read-only
                                    app.*                ← this app owns
                                  raw CSVs → .artifacts/<run_id>/  (gitignored)
```

Layering rule (already established in this codebase): **routes → services →
repositories → DB**. Routes never import SQLAlchemy. `sample_data.py` is
swapped out behind the same function seam it was built for; it keeps serving
`/live/*` indefinitely.

**Why a process pool:** an event-mode backtest is minutes of GIL-holding,
single-core CPU. Inline would freeze the API; a thread would starve the event
loop. `ProcessPoolExecutor(max_workers=2)` gives queueing and API
responsiveness with zero extra infrastructure. The FE polls
`GET /backtests/{id}` (its `useBacktest` hook already refetches while
non-terminal) — no SSE/websockets this phase.

---

## 4. Task list

Execute in order. Every task ends with: run
`venv/Scripts/python.exe -m pytest -q` (all green), commit to `dev`.

---

### Task 1 — Settings + database layer + `app` schema

**Goal:** the app connects to Postgres from typed settings; `app` schema and
tables exist; strategy registry seeded.

**Bigger picture:** every later task persists into or reads from these tables;
the run pipeline (task 6) claims and updates rows here.

**Files:**
- `src/core/config.py` — extend the existing `Settings` with `POSTGRES_*`
  fields (host, port, db, user, password, sslmode) loaded via
  `python-dotenv`; build two SQLAlchemy URLs with `sqlalchemy.engine.URL.create`
  (never string-format the password — it contains URL-special characters):
  `database_url_async` (`postgresql+asyncpg`) and `database_url_sync`
  (`postgresql+psycopg2`).
- `src/db/engine.py` — async engine + session factory for the API; sync
  engine factory for workers. `pool_pre_ping=True`, small pool (5).
- `src/models/` — SQLAlchemy 2.0 `DeclarativeBase` models, all with
  `__table_args__ = {"schema": "app"}`:

  | Model | Columns |
  |---|---|
  | `Strategy` | `key` text PK · `name` · `description` · `tags` JSONB · `universe` JSONB · `param_specs` JSONB · `kind` text (`builtin`\|`user`) · `class_path` text nullable (builtin only) · `storage_key` text nullable (user only — key into the strategy store) · `validation_run_id` UUID nullable FK → backtest_runs (the run that proved a user strategy works) · `status` text (`active`\|`validating`\|`failed_validation`\|`archived`) · `enabled` bool · `created_at` |
  | `BacktestRun` | `id` UUID PK default uuid4 · `name` · `strategy_key` FK · `status` text (`queued/running/completed/failed`) · `params` JSONB · `start_date` date · `end_date` date · `timeframe` text default `1d` · `symbol` text · `initial_capital` numeric · `final_equity` numeric nullable · `total_return` nullable · `sharpe` nullable · `max_drawdown` nullable · `progress_pct` smallint default 0 · `error_message` text nullable · `engine_version` text · `owner_id` UUID nullable · `cancel_requested` bool default false · `created_at` / `started_at` / `finished_at` timestamptz |
  | `RunMetrics` | `run_id` UUID PK/FK · `total_return` · `cagr` · `sharpe` · `sortino` · `max_drawdown` · `volatility` · `win_rate` · `profit_factor` · `total_trades` int · `extra` JSONB |
  | `RunEquityPoint` | `run_id` FK + `seq` int (composite PK) · `date` date · `equity` numeric · `benchmark` numeric nullable |
  | `RunTrade` | `run_id` FK + `seq` int (composite PK) · `symbol` · `side` text · `entry_date` · `exit_date` nullable · `entry_price` · `exit_price` nullable · `quantity` · `pnl` · `return_pct` · `fees` |

  `BacktestRun` additionally carries `purpose` text default `'user'`
  (`user`|`validation`) — a validation run for an uploaded strategy is a
  normal run through the whole pipeline, distinguishable so the list endpoint
  can filter it if the FE wants. There is **no separate drafts table**: an
  uploaded strategy is a `Strategy` row with `kind='user'`, and its lifecycle
  lives in `status`.

  FE status mapping (their Zod enum knows only `active|draft|archived`):
  `validating` and `failed_validation` both serialize as `draft`; the
  submission-result `message` explains which. Richer statuses flagged to the
  FE session in section 5.

  Indexes: `backtest_runs(created_at desc)`, `backtest_runs(status)`.
- `src/db/init.py` — `CREATE SCHEMA IF NOT EXISTS app` then
  `Base.metadata.create_all` (sync engine; called from a FastAPI lifespan
  hook and importable by scripts/tests). No Alembic yet — schema is young;
  add it when it stabilizes.
- `scripts/seed_strategies.py` — upsert 4 registry rows: `portfolio_1`
  (VolMomentum), `portfolio_2` (MomentumStrategy), `portfolio_3`
  (RegimeAdaptiveStrategy), `portfolio_dummy` (CrossoverRmiStrategy, marked
  `enabled=false` — test-only). `param_specs` follow the FE `ParameterSpec`
  shape exactly (`key/label/type/default/min/max`); derive sensible specs from
  each portfolio's `config.json` in MQSMaster (`LOOKBACK_DAYS`, capital etc.).
  `universe` = the config's `TICKERS`.
- Also in this task: measure `market_data` coverage (distinct tickers,
  min/max date per seeded universe ticker) with a short script, and **record
  the numbers in section 2 of this file**.
- `requirements.txt` add: `sqlalchemy>=2.0`, `asyncpg`, `psycopg2-binary`,
  `python-dotenv`, `pandas==2.2.2`, `numpy<=1.26.4`, `pytz`.

**Accept:** `python scripts/seed_strategies.py` exits 0; a psql-free check
script prints the 4 strategy rows; app boots with lifespan creating schema;
pytest green.

**Commit:** `Add database layer, app schema models, and strategy seed`

---

### Task 2 — Repositories + flip the read endpoints to the DB

**Goal:** `GET /strategies`, `GET /backtests`, `GET /backtests/{id}`,
`DELETE /backtests/{id}`, `POST /strategies` read/write Postgres.
`/live/*` untouched.

**Bigger picture:** the FE stops seeing fiction for everything backtest-shaped;
after task 6 writes real runs, they appear here with zero further changes.

**Files:**
- `src/repositories/strategies.py`, `src/repositories/runs.py` — async
  SQLAlchemy. Runs repo exposes `list_runs(filters, page, page_size)`,
  `get_run(id)` (joined-load metrics/equity/trades), `create_run(...)`,
  `delete_run(id)`, `request_cancel(id)`, and a `for_user(owner_id)` filter
  seam that currently no-ops (auth lands later).
- `src/services/backtests.py`, `src/services/strategies.py` — translate ORM
  rows into the **existing** Pydantic schemas (`src/schemas/`) — those match
  the FE and do not change. Strategy aggregates (`runCount`, `bestSharpe`,
  `bestReturn`, `lastRunAt`) computed with SQL aggregates in the repo, not
  Python loops.
- Routes: swap `sample_data` calls for service calls. `POST /strategies` in
  this task only persists the row (`kind='user'`, `status='validating'`,
  source held in the store from task 8 if already built, else a `source`
  staging column is acceptable **only** until task 9 replaces it) and returns
  `status="draft"` + a message that validation is pending. The real
  upload→validate→activate flow is task 9 — do not build it early, it needs
  the run pipeline.
- Tests: adjust contract tests where behavior legitimately changed (empty DB
  → empty list is valid). Add repo tests behind a `db` pytest marker that
  skip cleanly when the DB is unreachable.

**Accept:** with the seed applied, `GET /api/strategies` returns the 3 enabled
strategies from Postgres; `GET /api/backtests` returns `[]` (no runs yet);
pytest green.

**Commit:** `Back strategies and runs endpoints with Postgres repositories`

---

### Task 3 — Vendor the engine

**Goal:** the MQSMaster backtest engine lives in this repo under `engine/`,
imports rewritten, talking to our DB through a thin adapter — no MQSMaster
import anywhere.

**Bigger picture:** this is the algorithm the whole product exists to expose.
Vendoring (copy + own) is deliberate: task 4 modifies engine internals, and a
pip dependency on the trading repo would make that impossible.

**Copy map (from `C:/Users/user/OneDrive/Desktop/MQSMaster`):**

| From `src/...` | To |
|---|---|
| `backtest/backtest_engine.py`, `runner.py`, `executor.py`, `cost_model.py`, `utils.py` | `engine/core/` |
| `backtest/reporting.py`, `vectorized_backtest.py`, `vector_strategy_adapters.py`, `cscv.py`, `purged_kfold.py` | `engine/analytics/` |
| `backtest/data/backfill_cache/cache.py` | `engine/data/cache.py` |
| `portfolios/portfolio_BASE/`, `portfolio_1/`, `portfolio_2/`, `portfolio_3/`, `portfolio_dummy/` (each: `strategy.py` + `config.json`) | `engine/strategies/<same name>/` |
| `portfolios/order_interface.py`, `portfolio_interface.py`, `market_data_api.py`, `toolkit.py`, `common.py` | `engine/strategies/` |
| `portfolios/indicators/*.py` | `engine/indicators/` |

**Rules:**
- Rewrite all imports to absolute `engine.*`. MQSMaster's
  try-relative-then-absolute idiom dies here — single import path only.
- Indicators load dynamically by name via importlib in `portfolio_BASE`
  (`AddIndicator`/`RegisterIndicatorSet`) — update the module-path string it
  uses to `engine.indicators`, and keep snake_case-file → CamelCase-class
  naming (`relative_strength_index.py` → `RelativeStrengthIndex`).
- Config loading: `BasePortfolio` finds `config.json` via
  `inspect.getfile(cls)` sibling lookup. Copying each strategy folder whole
  preserves this; do not refactor it.
- `engine/data/db_adapter.py` — new, ~40 lines: class `EngineDBAdapter` with
  `execute_query(sql, params=None, fetch=False)` returning
  `{"status": "success", "data": [dict-rows]}` — the exact shape
  `MQSDBConnector` returns and `engine/core/utils.py` expects. psycopg2 +
  `RealDictCursor`, settings from `src/core/config.py`, autocommit reads.
  Do **not** copy `MQSDBConnector` itself (pool + env coupling not needed).
- Parquet cache path: point `engine/data/cache.py` at `<repo>/data/backfill_cache/`
  (gitignored). Optional warm-start: `scripts/seed_market_cache.py` copies the
  13 parquet files from `MQSMaster/src/backtest/data/backfill_cache/` if
  present.
- Record provenance: `engine/VENDORED_FROM` = MQSMaster commit SHA
  (`git -C <MQSMaster> rev-parse HEAD` at copy time).
- `engine/__init__.py` exposes `ENGINE_VERSION = "vendored-<shortsha>"`.

**Accept:** a throwaway script (`scripts/smoke_engine.py`, kept) instantiates
`portfolio_dummy` with the adapter, calls `fetch_historical_data` for its
tickers over a 10-trading-day window, prints a non-empty DataFrame shape from
the real `market_data` table. No MQSMaster path in `sys.path`. pytest green.

**Commit:** `Vendor MQSMaster backtest engine (SHA <shortsha>) with DB adapter`

---

### Task 4 — Engine contracts: results-as-data, errors, progress, cancel

**Goal:** a single function
`engine.run_single.run_single(request: RunRequest) -> RunResult` that runs ONE
portfolio and returns structured data. No CSVs-as-API, no swallowed
exceptions, no tqdm.

**Bigger picture:** this is the seam the worker (task 6) calls. Everything the
FE eventually renders — equity curve, trades, metrics, error banners, progress
bars — originates from this function's return value.

**Files:** `engine/contracts/` package (`run.py` with the dataclasses,
`errors.py` with `RunCancelled` / `NoMarketData`, `__init__.py` re-exporting
all of it), `engine/run_single.py`, surgical edits in
`engine/core/backtest_engine.py`, `engine/core/runner.py`,
`engine/analytics/reporting.py`.

**Contracts (dataclasses):**
```python
RunRequest:  run_id · strategy_key · class_path · start_date · end_date
             initial_capital · mode ("event"|"fast") · params dict
             artifact_dir · on_progress: Callable[[int, str], None]
             should_cancel: Callable[[], bool]
RunResult:   status ("completed"|"failed"|"cancelled") · error: str|None
             metrics: dict            # keys = RunMetrics columns
             equity_curve: list[(date, equity, benchmark|None)]
             fills: list[dict]        # raw executor fills
             final_equity: float|None
```

**Engine edits (keep them minimal and marked with `# VISUALIZER:` comments):**
1. `runner._run_event_loop`: replace tqdm with `on_progress(pct, stage)`
   computed from timestamp index; call `should_cancel()` per timestamp group,
   raise `RunCancelled(Exception)` when true.
2. `backtest_engine.run()` swallows per-portfolio exceptions (logs + continues)
   — in `run_single` path, re-raise instead. `RunResult.status="failed"`,
   `error=str(exc)` with class name.
3. Reporting: `aggregate_final_metrics` + rolling/monthly outputs still write
   CSVs into `request.artifact_dir` (`BACKTEST_OUTPUT_DIR` env override
   already exists — set it per-run), but the headline numbers now ALSO return
   as the `metrics` dict. Map to FE names: `total_return, cagr, sharpe,
   sortino, max_drawdown, volatility, win_rate, profit_factor, total_trades`
   (win_rate/profit_factor/total_trades computed in task 6 from paired
   trades if reporting lacks them — check `aggregate_final_metrics` first).
4. Empty-data guard: if `fetch_historical_data` returns an empty frame,
   raise `NoMarketData(tickers, start, end)` with the missing set in the
   message — never return an empty success.
5. `run_single` builds the portfolio class from `class_path`
   (`importlib`), monkeypatches nothing globally, and is **process-safe**:
   no module-level mutable state.

**Accept:** `tests/unit/test_run_single.py` — runs `portfolio_dummy`, event
mode, over a short window against the real DB (marked `db`): asserts
status=completed, monotonic progress calls ending at 100, non-empty
equity_curve, metrics keys complete; a second test over a window with a fake
ticker asserts status=failed with the ticker named in `error`; a third
cancels after the first progress call and asserts status=cancelled.

**Commit:** `Add run_single engine entrypoint: structured results, progress, cancellation`

---

### Task 5 — Trade pairing (fills → round trips)

**Goal:** pure function `pair_fills(fills) -> list[TradeRow]` turning the
engine's one-leg fills into the FE's round-trip `Trade` shape.

**Bigger picture:** the FE trade table and P&L histogram consume round trips
(`entryDate/exitDate/pnl/returnPct`). The engine only knows fills. This module
is the translation, and it must be boring and heavily tested because wrong
P&L numbers destroy trust in every chart above them.

**Files:** `src/services/trade_pairing.py`,
`tests/unit/test_trade_pairing.py`.

**Rules:** FIFO per ticker. BUY opens/extends long, SELL closes long first
then opens short (and mirror). Partial fills split lots; each closed lot emits
one round trip (`pnl = (exit-entry)*qty` sign-adjusted for shorts;
`return_pct = pnl / (entry*qty)`; fees pro-rated if present). Open lots at end
→ rows with `exit_date=None, exit_price=None`, pnl = unrealized 0 (leave 0,
document). Deterministic `seq` ordering.

**Accept:** unit tests cover: simple long round trip, partial close, short
round trip, flip long→short in one fill, unclosed remainder. pytest green (no
DB needed).

**Commit:** `Add FIFO fill-pairing service with unit tests`

---

### Task 6 — Job manager + persistence of results

**Goal:** submitted runs execute in worker processes; progress, results,
errors, and cancellation all land in `app.*` tables.

**Bigger picture:** this is the machinery behind the Run Backtest button —
API stays responsive while the engine burns a core.

**Files:** `src/workers/job_manager.py`, `src/workers/run_job.py`,
`src/workers/reconciler.py`.

**Requirements:**
- `JobManager` singleton created lazily in the FastAPI lifespan (never at
  import — Windows spawn + `uvicorn --reload` double-import would fork-bomb).
  `ProcessPoolExecutor(max_workers=settings.max_concurrent_runs, default 2)`.
- `run_job(run_id)` executes **in the worker process**: own sync DB
  connection; claim with
  `UPDATE app.backtest_runs SET status='running', started_at=now() WHERE id=%s AND status='queued'`
  — zero rows affected → someone else claimed → return (idempotency).
- `on_progress`: throttled UPDATE of `progress_pct` (≥1s between writes).
  `should_cancel`: SELECT `cancel_requested` (same throttle).
- On completion: single transaction writes `run_metrics`, bulk-insert
  `run_equity_points` (`executemany`/`COPY`), paired `run_trades` (task 5),
  updates run row (`status`, `final_equity`, denormalized `total_return`,
  `sharpe`, `max_drawdown`, `finished_at`, `progress_pct=100`).
- On exception: `status='failed'`, `error_message` (truncate 2000 chars).
- Artifacts: engine CSVs land in `.artifacts/<run_id>/` (dir per run,
  gitignored already via `.artifacts/`).
- `reconciler.py`: at startup, any run stuck `running` (process died) →
  `failed`, message "Interrupted by server restart". Called from lifespan.
- Equity curve size guard: event mode records per poll-interval; downsample to
  daily last-value before insert (FE charts are daily; keeps rows ≈ trading
  days).

**Accept:** integration test (marker `db`): create run row directly, invoke
`run_job(run_id)` synchronously in-process, assert row transitions and
metrics/equity/trades rows exist and are internally consistent
(`final_equity == last equity point`). pytest green.

**Commit:** `Add process-pool job manager persisting engine results`

---

### Task 7 — The Run Backtest endpoint

**Goal:** `POST /api/backtests` — the reason this application exists.

**Request** (Pydantic, camelCase aliases, mirrors what the FE New Run form
will send — this shape is already communicated to the FE session):
```json
{
  "name": "Regime adaptive — 2025 H1",
  "strategyKey": "portfolio_3",
  "startDate": "2025-01-02",
  "endDate": "2025-06-30",
  "initialCapital": 1000000,
  "mode": "event",
  "params": {"LOOKBACK_DAYS": 90}
}
```

**Behavior:**
1. Strategy must exist and be `enabled` → else 422 with plain message.
2. Dates: ISO, `start < end`, window ≤ `MAX_BACKTEST_WINDOW_DAYS` (env,
   default 1825), `initialCapital > 0`.
3. `params` validated against the strategy's `param_specs` (unknown key →
   422 naming it; type/min/max enforced). Params overlay the strategy's
   `config.json` at run time (merge in `run_single`, task 4 already accepts
   `params`).
4. Insert run row: `status=queued`, `symbol` = single ticker if universe has
   one else `"MULTI"`, `timeframe="1d"`, `engine_version` from
   `engine.ENGINE_VERSION`.
5. Submit to JobManager. **Submit failure must not lose the row** — wrap; on
   executor rejection mark run `failed`.
6. Return **`202`** with the full `BacktestSummary` payload (FE can insert it
   into its list cache immediately).
7. `DELETE /backtests/{id}`: terminal run → delete rows + artifact dir;
   `queued/running` → set `cancel_requested=true`, return 204 (run becomes
   `failed`/`cancelled` via engine hook). Document both in the docstring.
8. `GET /backtests/{id}` response gains `progressPct` (int, nullable) — 
   **additive** field; FE Zod ignores unknown keys, and the FE session is
   notified to render it.

**Accept:** contract tests: 202 + immediate GET shows `queued|running`;
polling (short dummy run) reaches `completed` with metrics + equity + trades
present; invalid strategy 422; bad params 422 naming the key; DELETE on
running sets cancel and the run terminates; pytest green.

**Commit:** `Add POST /backtests run submission with validation and 202 flow`

---

### Task 8 — Strategy store (S3-shaped, local for now)

**Goal:** one small interface every strategy read/write goes through, so the
later S3 move is a new implementation, not a refactor.

**Bigger picture:** the user asked for uploaded strategies to live in an S3
bucket. The bucket does not exist yet and must not block the flow — so the
call sites are written against the S3 shape today, backed by local disk.

**Files:** `src/integrations/strategy_store.py`,
`tests/unit/test_strategy_store.py`.

**Interface (mirror S3 semantics exactly — keys, not paths):**
```python
class StrategyStore(Protocol):
    def put(self, key: str, filename: str, content: str) -> None: ...
    def get(self, key: str, filename: str) -> str: ...          # KeyError if absent
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def materialize(self, key: str, dest_dir: Path) -> Path: ...
    # ^ downloads every file under the key into dest_dir and returns it —
    #   this is what the engine loader consumes (S3 impl will download;
    #   local impl copies). Keys look like "strategies/<strategy_key>/".
```
- `LocalStrategyStore(root=".strategy_store/")` — root gitignored; layout
  `<root>/strategies/<strategy_key>/strategy.py` (+ `config.json`). This
  layout is deliberately identical to `engine/strategies/<portfolio_n>/`, so
  the engine's config-by-sibling-file discovery works unchanged on
  materialized user strategies.
- `S3StrategyStore` — **stub only**: class exists, constructor takes bucket
  name, every method raises `NotImplementedError("S3 backend arrives with
  infrastructure")`. Selection via `STRATEGY_STORE_BACKEND=local|s3` in
  settings, default `local`.

**Accept:** unit tests for put/get/exists/delete/materialize round-trip on a
tmp dir; pytest green.

**Commit:** `Add S3-shaped strategy store with local backend`

---

### Task 9 — User strategy pipeline: upload → validation backtest → activate → rerun

**Goal:** the full loop the product owner described: upload a `.py`, the
system proves it works by running a backtest, stores it, and from then on the
student selects it and reruns like any built-in.

**Bigger picture:** this is what makes the platform a *platform* rather than a
viewer for nine fixed portfolios. It reuses every prior task: store (8), run
pipeline (6/7), engine entrypoint (4), persistence (1/2).

**Security disclosure (do not skip, do not soften):** validating means
**executing user-supplied Python** in a worker process that holds admin
database credentials. Product decision is to ship functional-first — but the
implementing model must include the cheap guardrails and the loud comments:
these are speed bumps, not a sandbox; real isolation (container, no-egress,
scoped DB role) is deferred work, recorded in section 7.
Guardrails now: (a) AST scan rejecting imports outside an allowlist
(`engine.*`, `pandas`, `numpy`, `math`, `datetime`, `typing`,
`collections`, `statistics`) and rejecting `exec`/`eval`/`__import__`/
`open`/`subprocess`/`os.` usage; (b) wall-clock timeout on validation runs
(`VALIDATION_TIMEOUT_SECONDS`, default 600 — enforced by the existing cancel
mechanism from a watchdog timer); (c) validation window kept short.

**Flow (`src/services/strategy_validation.py` + rework of
`src/api/routes/strategies.py`):**
1. `POST /strategies` body (unchanged FE shape): `name`, `description`,
   `source`, `filename`. Plus optional `config` dict (tickers/interval/
   lookback) — FE not sending it yet; default config used when absent:
   `{TICKERS: ["AAPL","MSFT"], INTERVAL: 60, LOOKBACK_DAYS: 30, WEIGHTS: equal, DATA_FEEDS: ["market_data"]}`.
2. AST guardrail scan → violation = 422 naming the offending line, nothing
   stored.
3. Source must define exactly one `BasePortfolio` subclass (checked by AST
   class-def scan for the name in bases; ambiguous/zero = 422).
4. Store: `put("strategies/<key>/", "strategy.py", source)` +
   generated `config.json`. Key = slugified name + short uuid.
5. Insert `Strategy` row: `kind='user'`, `status='validating'`,
   `storage_key`, `enabled=false`.
6. Create a `BacktestRun` with `purpose='validation'`, short window (last 30
   calendar days of available data), small capital, and submit through the
   normal JobManager. Response to the FE **immediately**: existing
   `StrategySubmissionResult` shape, `status="draft"`,
   `message="Validation backtest started — the strategy activates when it passes."`
7. Worker side (`run_job`): when the run's strategy is `kind='user'`, the
   loader materializes the store key into a per-run temp dir, imports
   `strategy.py` via `importlib.util.spec_from_file_location`, finds the
   `BasePortfolio` subclass by inspection, and hands it to `run_single`
   exactly like a built-in class. (Loader lives in
   `engine/strategies/user_loader.py`; ~50 lines.)
8. On validation run completion, a post-run hook (in `run_job`, keyed off
   `purpose='validation'`): run `completed` → strategy `status='active'`,
   `enabled=true`, `validation_run_id` set; run `failed` →
   `status='failed_validation'`, error preserved on the run row the user can
   open.
9. Rerun path needs **no new code**: `POST /backtests` with the user
   strategy's key already works — task 7's endpoint checks `enabled`, and the
   worker loader (step 7) branches on `kind`. Add one contract test proving
   it.

**Accept (marker `db`):** end-to-end test — POST a valid minimal strategy
(template from `engine/strategies/portfolio_dummy` adapted), poll until the
strategy row goes `active`, then `POST /backtests` against it and see
`completed` with metrics; a second test POSTs source with `import os` and
gets 422; a third POSTs source raising in `OnData` and sees
`failed_validation` with the error retrievable. pytest green.

**Commit:** `Add user strategy upload, validation-by-backtest, and rerun from store`

---

### Task 10 — Docs, env template, requirements truth

**Goal:** a fresh clone + `.env` + two commands = working app. No tribal
knowledge.

- `.env.example`: exactly the `POSTGRES_*` block (no secrets), worker knobs
  (`MAX_CONCURRENT_RUNS`, `MAX_BACKTEST_WINDOW_DAYS`), artifact dir.
- `README.md`: update endpoint table (add POST /backtests + example curl from
  task 7), document the strategy upload→validation→activate lifecycle and the
  store layout (task 8–9), replace the "sample data" paragraph — backtest
  group is now real, `/live/*` remains sample and says so; run pipeline
  diagram; startup:
  `uvicorn server:app --reload --port 8000` (reload is safe because the pool
  is lifespan-lazy — state why).
- `requirements.txt`: complete and pinned where it matters (`pandas==2.2.2`,
  `numpy<=1.26.4`); everything imported must be listed.
- Update **this file**: mark tasks done, record `market_data` coverage
  numbers (task 1), note deviations.

**Accept:** on a machine with only `.env`: `pip install -r requirements.txt`,
`python scripts/seed_strategies.py`, `uvicorn server:app` → submit a dummy
run via curl → completed with results. pytest green.

**Commit:** `Document run pipeline; finalize env template and requirements`

---

## 5. Message for the FE session (forward verbatim)

1. **`POST /api/backtests` exists after task 7** — request shape in section 4
   task 7. Field names final: `strategyKey`, `startDate`, `endDate`,
   `initialCapital`, `mode`, `params`. Returns `202` + full `BacktestSummary`.
2. **Progress:** poll `GET /api/backtests/{id}`; new nullable `progressPct`
   field (0–100). Render a bar when non-null and status=running.
3. **Cancelled runs** surface as `status="failed"` with
   `errorMessage="Cancelled by user"` until you add `cancelled` to
   `backtestStatusSchema` — your call when.
4. **`symbol`** on multi-ticker runs is the literal string `"MULTI"`; ticker
   list rides in `parameters`. Propose `symbols: string[]` schema evolution
   when convenient.
5. **DELETE semantics:** on a running run = cancel (row remains, terminal);
   on a terminal run = permanent delete. Confirm the UI matches.
6. **Auth:** backend reserves `owner_id`; tell us which Supabase JWT claim to
   trust (`sub` assumed) and the header format when your auth lands.
7. **Strategy upload is becoming real (tasks 8–9).** `POST /strategies` keeps
   its exact request/response shape, but semantics change: submission kicks
   off a **validation backtest**; the strategy appears in `GET /strategies`
   as `draft` while validating, flips to `active` when the run passes, stays
   `draft` on failure (message says why). Your existing
   invalidate-list-on-submit already picks this up — consider polling the
   list briefly after submit, or ask us for `GET /strategies/{key}` if you
   want a detail poll. Longer term: `validating`/`failed` statuses in
   `strategyStatusSchema` would let you show real state.
8. **Sample strategy template:** deferred but planned — we will expose the
   expected `.py` shape (a `BasePortfolio` subclass with `OnData(context)`)
   for your editor's "Reset to template" button; coordinate when tasks 8–9
   land.

## 6. Risks / gotchas (read before task 3 and 6)

- **Windows spawn:** worker code must be importable without side effects;
  everything under `if TYPE_CHECKING` / functions; pool built in lifespan only.
- **OneDrive IO:** parquet cache + artifacts under a synced folder are slow;
  keep artifact CSVs minimal (skip minute-by-minute file if
  `_minute_resample_too_large` — reporting already guards this).
- **DB is remote (university network):** engine backfill queries can be
  minutes on first run per ticker set; the parquet cache makes rerun fast.
  First-run slowness is expected — progress stage label should say
  "loading data".
- **`sslmode=require` fails** against this server; `prefer` works. Already
  encoded in config default.
- **pandas 2.2.2 API** — no `Series.map(na_action=...)` newness beyond 2.2,
  no pandas 3 idioms. Engine code is validated on these pins.
- **Do not "fix" engine math** while vendoring — copy faithfully, adapt only
  the seams listed in task 4.
- **User code execution (task 9) is the biggest risk in the plan.** The AST
  allowlist is a speed bump a determined student walks around; the worker
  process holds admin DB credentials. Acceptable only because the audience is
  a small authenticated club and the product decision is functional-first.
  Mark every guardrail with a comment saying it is not a security boundary.

## 7. Deferred — explicitly not this plan, do not build early

| Item | Arrives when |
|---|---|
| Real `S3StrategyStore` (bucket, IAM, presigned URLs) | Infrastructure repo provisions the bucket; swap = implement the task-8 protocol |
| Sample/template `.py` endpoint for the FE editor | After tasks 8–9 prove the upload loop |
| Real sandboxing for user code (container isolation, no-egress network, scoped DB role instead of admin creds) | Before the app is exposed beyond the club |
| Portfolios 4–8 as built-ins (RBP/screener/NLP dependency chains) | After the v1 pipeline is stable |
| `/live/*` backed by real trading tables | Separate product decision — read-only queries exist in this DB but live trading is not this app's scope |
| Auth enforcement (`owner_id` filters, JWT verification) | Parallel session's auth work merges |
| SSE / websocket progress (replaces polling) | Only if polling proves insufficient |
| Alembic migrations | First schema change after other people depend on `app.*` |
