# Backend Build Plan — Backtest Visualizer

**Session scope:** backend only. Real API endpoints, a real database, and the
MQSMaster backtest engine adapted to run inside this application. Auth is
explicitly out of scope (handled in a parallel session — Supabase + OAuth, not
yet committed); everything here leaves clean seams for it to plug into.

**Source of truth for the contract:** `Backtest_Visualiser_FE/src/features/*/types.ts`
(Zod schemas) and the `*-api.ts` modules (endpoints called). The FE parses every
response; a wrong key name is a hard runtime failure in the browser. The
existing `tests/unit/test_api_contract.py` guards this and must stay green
throughout.

---

## 1. Where things stand

| Piece | State |
|---|---|
| 13 routes under `/api` | Built, serving deterministic **sample data** |
| Pydantic schemas mirroring the FE Zod types | Built (`src/schemas/`) |
| Contract tests | 18 passing |
| Database | None |
| Engine | Untouched, lives in `MQSMaster/src/backtest` |
| Run submission (`POST /backtests`) | Does not exist on either side yet |

The FE calls exactly these endpoints today (verified by grep over its
`*-api.ts` modules — unchanged since the routes were built):

```
GET    /backtests                 GET /live/portfolios
GET    /backtests/{id}            GET /live/portfolios/{id}
DELETE /backtests/{id}            GET /live/portfolios/{id}/equity
GET    /strategies                GET /live/portfolios/{id}/composition
POST   /strategies                GET /live/portfolios/{id}/executions
GET    /live/system/status        GET /live/portfolios/{id}/correlations
GET    /live/system/logs
```

**Scope decision:** the `/live/*` group describes the *live trading system*
(MQS Master views). Live trading is not part of the backtest application —
those endpoints stay on generated sample data this session, clearly marked.
The backtest group (`/backtests`, `/strategies`) becomes real end to end.

---

## 2. Target architecture

```
                 POST /backtests
Frontend ──────────────────────────► FastAPI (src/api)
   │                                    │ validate params against strategy schema
   │ poll GET /backtests/{id}           │ insert run row (status=queued)
   │                                    ▼
   │                              Job manager (src/workers)
   │                              ProcessPoolExecutor, N=2
   │                                    │ status=running, progress updates
   │                                    ▼
   │                              engine/ (vendored from MQSMaster)
   │                              BacktestEngine + BacktestRunner
   │                                    │ market data: parquet cache (+ optional
   │                                    │ MQS Postgres for missing ranges)
   │                                    ▼
   └── reads results ◄──────────  PostgreSQL (SQLAlchemy)
                                  runs / metrics / equity points / trades
                                  raw CSVs → .artifacts/<run_id>/
```

Request flow stays layered: **routes → services → repositories → DB**. Routes
never touch SQLAlchemy directly; `sample_data.py` gets swapped out behind the
same function seam it was built for.

### Why a process pool and not inline requests

An event-mode backtest steps timestamp-by-timestamp — minutes of single-core,
GIL-holding work. Run inline it would freeze every other request; run in a
thread it still starves the event loop. `ProcessPoolExecutor(max_workers=2)`
keeps the API responsive, gives natural queueing, and needs zero infrastructure
(no Redis/SQS — right-sized for a club deployment). The FE already polls:
`BacktestStatus` includes `queued | running`, and `useBacktest` refetches until
terminal.

---

## 3. Database

**Engine:** PostgreSQL. **Recommendation: the Supabase project's Postgres** —
auth already lives there, one managed instance, nothing to install, and the
`owner_id` link to `auth.users` becomes trivial when auth lands. Standard
`DATABASE_URL` in `.env` means a local Postgres or Docker service works
identically if the club prefers. **SQLAlchemy 2.0**, async engine in the API,
sync engine in the worker processes (workers are synchronous by nature).

Schema (`app` schema, created via `create_all` at startup for now — Alembic
gets added when the schema stabilizes, per the earlier decision to drop the
migrations folder):

```
strategies            key PK · name · class_path · description · tags[]
                      universe[] · param_specs jsonb · enabled · status
backtest_runs         id uuid PK · name · strategy_key FK · status
                      params jsonb · start_date · end_date · timeframe
                      symbol (display label: first ticker or "MULTI")
                      initial_capital · final_equity · total_return
                      sharpe · max_drawdown       <- denormalized for list view
                      progress_pct · error_message · engine_version
                      owner_id uuid NULL          <- ready for auth, unused now
                      created_at · started_at · finished_at
run_metrics           run_id PK/FK · total_return · cagr · sharpe · sortino
                      max_drawdown · volatility · win_rate · profit_factor
                      total_trades · extra jsonb
run_equity_points     run_id FK · seq · date · equity · benchmark
run_trades            run_id FK · seq · symbol · side · entry/exit date+price
                      quantity · pnl · return_pct · fees
strategy_drafts       id · name · description · source text · filename
                      status=draft · created_at        <- POST /strategies target
```

Design rules carried over from the platform plan: wide columns for the metrics
the UI sorts on, `jsonb` for the long tail; `engine_version` recorded on every
run (a result is uninterpretable without the code that produced it); index on
`(status)` for the reconciler and `(created_at DESC)` for the list.

---

## 4. Engine adaptation (the core of the session)

### 4.1 How it comes in

**Vendor the needed modules into `engine/`** (copy, not pip-install): the
engine must be modified (progress, cancellation, error propagation) and this
repo owns the fork. Provenance pinned by recording the MQSMaster commit SHA in
`engine/VENDORED_FROM` and on every run row.

What gets vendored, mapped to the folders that already exist:

| From MQSMaster | To | Why |
|---|---|---|
| `src/backtest/backtest_engine.py`, `runner.py`, `executor.py`, `cost_model.py`, `utils.py` | `engine/core/` | Simulation kernel |
| `src/backtest/reporting.py`, `vectorized_backtest.py`, `vector_strategy_adapters.py`, `cscv.py`, `purged_kfold.py` | `engine/analytics/` | Metrics + fast mode |
| `src/backtest/data/backfill_cache/cache.py` | `engine/data/` | Parquet cache layer |
| `src/portfolios/portfolio_BASE`, `portfolio_1..3`, `portfolio_dummy`, `order_interface.py`, `portfolio_interface.py`, `market_data_api.py`, `toolkit.py`, `common.py` | `engine/strategies/` | Strategy classes + the `StrategyContext` seam |
| `src/portfolios/indicators/*` | `engine/indicators/` | Loaded by name via importlib |

**v1 strategy set: portfolios 1, 2, 3 (+ dummy for tests).** Portfolios 4-8
drag heavier dependency chains (RBP model, screener, NLP feeds); they join once
the pipeline works. The registry is data-driven so adding one is a row + a
vendored folder, not a code change.

Import rewrite: the vendored tree uses `engine.*` absolute imports only — kills
MQSMaster's dual-import idiom, which exists for a repo layout we do not have.

### 4.2 The five refactors (each small, each load-bearing)

1. **Data access.** `utils.fetch_historical_data` is already cache-first
   (parquet per ticker, DB only for missing ranges). Formalize the fallback as
   a `MarketDataSource` protocol in `engine/contracts/`:
   - `ParquetOnlySource` (v1 default): serves from the cache; a missing
     ticker/range **fails the run with an actionable message** ("no data for
     NVDA 2024-01..2024-03") instead of silently proceeding.
   - `MQSPostgresSource` (optional): wraps the read-only MQS `market_data`
     connection when creds exist in `.env`, and back-fills the cache exactly
     as today.

   Seed data: `scripts/seed_market_data.py` copies the 13 parquet files
   (AAPL, MSFT, NVDA, TSLA, AMZN, GLD, TLT, JPM, CAT, UNH, WMT, XOM, _VIX)
   from the MQSMaster cache into this repo's gitignored `data/` dir.
2. **Results as data.** `BacktestEngine.run()` returns only a trade-log list;
   metrics exist solely as CSVs. New `engine/contracts/RunResult`:
   `status · metrics dict · equity_curve · fills · error`. Reporting still
   writes its CSVs (to `.artifacts/<run_id>/` via the existing
   `BACKTEST_OUTPUT_DIR` override) — but the numbers come back as objects and
   land in Postgres.
3. **Errors surface.** `run()` currently catches per-portfolio exceptions,
   logs, and continues — a hosted API would report *succeeded with empty
   results* for a crashed run. The single-portfolio entrypoint re-raises; the
   worker marks the run `failed` with the message.
4. **Progress + cancellation.** Replace the tqdm bar in `_run_event_loop` with
   an injected `on_progress(pct, stage)` callback (worker throttles writes to
   the run row, about 1/sec max). The same hook checks a cancel flag and raises
   `RunCancelled` — cooperative cancellation, checked per timestamp group.
   `DELETE /backtests/{id}` on a queued/running run sets the flag.
5. **One job = one portfolio.** The ProcessPool fan-out in `main_backtest.py`
   is a CLI concern and does not come over. The worker calls a new
   `engine/run_single.py` entrypoint; parallelism belongs to the job manager.

### 4.3 Contract friction to resolve (needs FE awareness)

The FE `Trade` is a **round trip** (entry/exit, pnl, returnPct). The engine
logs **fills** (one leg). The worker pairs fills FIFO per ticker into round
trips at completion time; unclosed positions become open trades
(`exitDate: null` — the Zod schema already allows it). Fills also persist raw
in the artifacts CSV, so nothing is lost by the pairing.

The FE summary has `symbol: string` (singular) — engine runs are multi-ticker
portfolios. v1: `symbol` = the ticker for single-ticker runs, `"MULTI"`
otherwise; the real ticker list rides in `parameters`. Flagged to the FE
session as a schema evolution candidate (`symbols: string[]`).

---

## 5. New/changed endpoints

Everything existing keeps its exact shape. New:

```
POST /backtests            202 {id}   body: { name, strategyKey, startDate,
                                      endDate, initialCapital,
                                      mode: "event"|"fast", params: {...} }
                                      validated against the strategy's
                                      param_specs
DELETE /backtests/{id}     204        completed run -> delete rows + artifacts
                                      queued/running -> cancel, then mark
GET  /backtests/{id}       unchanged shape; status/progress now real
                                      (FE polls this — no SSE this session)
```

`GET /backtests`, `GET /backtests/{id}`, `GET /strategies` flip from
`sample_data` to repositories. `POST /strategies` writes `strategy_drafts`
(still stores source only — sandboxed execution remains out of scope, response
keeps saying so).

Status vocabulary: FE knows `queued|running|completed|failed`. Cancelled runs
map to `failed` with `error_message="Cancelled by user"` until the FE adds the
enum member (flagged below).

---

## 6. Order of work

| # | Deliverable | Proof |
|---|---|---|
| 1 | DB layer: settings, async+sync engines, models, `create_all`, strategy seed | app boots, tables exist |
| 2 | Vendor engine into `engine/`, import rewrite, parquet seed script | `engine/run_single.py` runs portfolio_dummy over cached data from a bare script |
| 3 | Contracts: `MarketDataSource`, `RunResult`, progress/cancel hooks wired through runner | smoke test asserts progress ticks + clean error on missing data |
| 4 | Job manager: pool, submit, claim (queued->running conditional update), heartbeat progress writes, startup reconciler (orphaned running->failed) | two concurrent runs queue correctly |
| 5 | `POST /backtests` + params validation; worker persists metrics/equity/trades; FIFO pairing | run submitted via HTTP finishes and reads back |
| 6 | Flip GET routes to repositories; `/live/*` stays sample | contract tests green untouched |
| 7 | Tests: worker integration (tiny 5-day window), pairing unit tests, cancellation test | full suite green |
| 8 | `.env.example`, README endpoint table, requirements pins (`pandas==2.2.2`, `numpy<=1.26.4` — engine hard requirement), compose Postgres service (optional) | fresh-clone instructions work |

Each step commits separately to `dev`.

## 7. Risks / gotchas

- **Windows + ProcessPoolExecutor:** spawn (not fork) — pool created lazily
  inside the app factory, guarded so uvicorn `--reload` does not double-spawn.
- **Engine pins:** pandas `2.2.2` / numpy `<=1.26.4` are MQSMaster
  requirements; both have cp312 wheels, but the pin goes in requirements.txt
  verbatim so nobody upgrades the engine's math out from under it.
- **OneDrive:** parquet + artifact IO under a synced folder is slow; artifacts
  dir stays small and gitignored (`.artifacts/`).
- **Timezones:** all engine timestamps are `America/New_York`; the API layer
  emits ISO dates and never re-zones.
- **Auth landing later:** `owner_id` column exists now; run queries get a
  `for_user()` seam in the repository from day one, returning everything until
  auth wires in.

## 8. Needed from the FE session (forward this list)

1. **`POST /backtests` request shape** — proposed in section 5; confirm field
   names (`strategyKey` vs `strategyId`) before they build the New Run form.
2. **`cancelled` status** — add to `backtestStatusSchema` enum when convenient;
   until then cancelled arrives as `failed` + message.
3. **`symbol` vs multi-ticker** — see section 4.3; propose `symbols: string[]`
   later.
4. **Polling is the progress mechanism** — `GET /backtests/{id}` carries
   `status` plus a new nullable `progressPct` field (additive; Zod ignores
   unknown keys by default, but flag it so they can render a progress bar).
5. **Supabase JWT claims** — which claim becomes `owner_id` (`sub` assumed);
   backend already reserves the column.
6. **Delete semantics** — DELETE on a running run cancels it (then it stays
   listed as failed/cancelled) vs only deletable when terminal. Backend
   implements cancel-then-mark; shout if the UX assumes hard delete.
