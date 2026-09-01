# Readiness — what works, what is measured, what is missing

A snapshot taken **2026-09-01** against the live MQS database and the three
sibling repositories. Rerun the numbers any time with:

```bash
venv/Scripts/python.exe scripts/smoke_db.py     # exit 0 = every check passed
```

- [Database wiring](#database-wiring)
- [Market data: horizon and freshness](#market-data-horizon-and-freshness)
- [Gaps, ranked](#gaps-ranked)
- [What was checked and found fine](#what-was-checked-and-found-fine)

---

## Database wiring

Everything the application uses to reach PostgreSQL, measured from a laptop on
the public internet (medians; the first connection pays ~90 ms of setup):

| Check | Result | Latency |
| --- | --- | --- |
| TCP reach `munquant.cair.mun.ca:25060` | OK | 7–33 ms |
| `psycopg2` connect (worker path) | OK — PostgreSQL 17.6, as `admin` | ~90 ms |
| `SELECT 1` round trip | OK | **5 ms** |
| `app` schema — 5 tables + `heartbeat_at` | OK — strategies=5, runs=5, equity_points=434, trades=22 | — |
| `market_data` index on `(ticker, timestamp)` | OK — `market_data_ticker_timestamp_key` | — |
| Engine's daily-bar query, 45 days, 2 tickers | OK — 17 trading days each | **55 ms** |
| SQLAlchemy sync (worker) | OK | 26 ms |
| SQLAlchemy async (API) | OK | 36 ms |
| Vendored engine adapter `execute_query` | OK — `{"status": "success", ...}` | 39 ms |

**Verdict: wired, fast, and both drivers agree.** A 5 ms round trip means the
throttled progress/heartbeat writes are negligible, and the engine's own query
shape is index-backed — a run's time goes to simulation, not to the database.

Two facts about the server that matter downstream:

- **SSL is off on the server** (`SHOW ssl` → `off`). `sslmode=require` fails
  outright; `prefer` connects **in plaintext**. Fine on the university network,
  not across the public internet — see gap 2.
- The table is **250 GB**. The `(ticker, timestamp)` index is what keeps the
  engine's query at 55 ms; anything that bypasses it scans a quarter terabyte.

---

## Market data: horizon and freshness

Per-ticker first and last bar, for every ticker a seeded strategy uses
(index-backed, ~5–30 ms each):

| Ticker | First bar | Last bar | Note |
| --- | --- | --- | --- |
| AAPL, TSLA, AMD, MSFT, NVDA, AMZN | 2019-11-11 | **2026-07-15** | portfolio_1 / portfolio_2 universe |
| WMT | 2020-01-02 | 2026-07-15 | |
| JPM | 2020-01-02 | 2026-06-22 | |
| CAT | 2020-01-02 | 2026-05-26 | |
| XOM, UNH | 2020-01-02 | 2026-05-21 | |
| GLD, ^VIX | 2020-01-0x | 2026-05-20 | |
| **TLT** | 2019-11-11 | **2025-11-07** | caps portfolio_3's whole window |

**The ingestor has been stopped since 2026-07-15** — 48 days at the time of
writing, and no ticker has a bar in the last 7 days. Backtests over history
work exactly as before; anything a student expects to be "recent" is not.
This is the live-trading stack's ingestor (`MQSMaster`), not this repository,
but it decides what this product can show.

History is intact (2019 onward, not yet pruned) — but see gap 3.

---

## Gaps, ranked

Ordered by what blocks a deploy first. "Where" says which repository owns the
fix; **this repo** items are actionable here.

### 1. Uploaded strategies and run artifacts live on ephemeral disk — **blocks deploy**

`.strategy_store/`, `.artifacts/` and the parquet cache are local directories.
On Fargate that disk is per-task and gone on every deploy or restart: a
student's uploaded strategy would validate, activate, and **vanish** at the next
release, leaving a registry row that points at nothing. `S3StrategyStore` is a
stub by design; the infra already carries an `artifact_bucket_arn` grant but
provisions no bucket.

- Where: **this repo** (implement `S3StrategyStore` behind the existing
  protocol; artifact sink to S3), **MQS_AWS_INFRA** (create the bucket, set
  `artifact_bucket_arn`).

### 2. The infra requires `sslmode=require`; the server cannot do SSL — **blocks deploy**

`market_data_secret_values` is validated to `require|verify-ca|verify-full`,
correctly — the password would otherwise cross the public internet in clear.
The CAIR instance has SSL off, so a deploy built that way cannot connect at all.
This code does not downgrade a required-SSL deploy (it must fail loudly).

- Where: **CAIR / MQSMaster ops** (enable SSL on the instance), or the infra
  relaxes the rule *and* accepts plaintext credentials on the wire. The first
  is the right answer.

### 3. Data retention will delete most of the history — **product decision needed**

`MQSMaster` `main` (21 commits past the vendored SHA) adds a **retention
pruner**: `MARKET_DATA_RETENTION_DAYS=545` (~18 months), deleting older rows
once deployed. This app promises windows up to `MAX_BACKTEST_WINDOW_DAYS=1825`
(5 years) and the seeded strategies were written against 2019-onward data.

- Where: **MQSMaster** (retention policy), **this repo** (cap the window to the
  retention horizon, or drive the run form from `/market-data/coverage` — which
  the frontend already calls).

### 4. Infra environment contract was written against the old `.env`

The task definition injects `API_V1_PREFIX=/api/v1`,
`MAX_CONCURRENT_RUNS_PER_USER`, `RATE_LIMIT_PER_MINUTE`, `HOST`, `PORT` and
`MARKET_DATA_*` credentials. This code reads `API_PREFIX` (`/api`, the
frontend contract), `MAX_CONCURRENT_RUNS` (pool-wide), no rate limit, and
`POSTGRES_*`.

- **Fixed here:** `POSTGRES_*` now falls back to `MARKET_DATA_*`, so a container
  wired either way reaches the database. Health path `/api/v1/health` is
  served alongside `/api/health`, so the ALB check passes.
- Still ignored (harmless, but the infra thinks it is configuring something):
  `API_V1_PREFIX`, `MAX_CONCURRENT_RUNS_PER_USER`, `RATE_LIMIT_PER_MINUTE`.
- Where: **MQS_AWS_INFRA** `locals.tf` — rename to `MAX_CONCURRENT_RUNS`, drop
  the two dead keys, or pass them through `extra_environment` once they exist.

### 5. Backtests run inside the API task; the infra sizes the task as API-only

`task_cpu` is documented as "the API stays small and warm; backtests belong in
a separate worker task". They do not — the process pool lives in the API task.
At 1 vCPU with `MAX_CONCURRENT_RUNS=2`, two students starve the API.

- Where: **MQS_AWS_INFRA** (`extra_environment = { MAX_CONCURRENT_RUNS = "1" }`
  as the immediate fix; 2 vCPU or a separate worker service later).

### 6. No CI/CD in either repository

`MQS_AWS_INFRA/README.md` describes `.github/workflows/deploy.yml`; only
`secret-scan.yml` exists. This repo's `.github/workflows/` holds a `.gitkeep`.
Nothing runs the 261 tests on a push and nothing builds or ships the image.

- Where: **this repo** (test + build/push workflow), **MQS_AWS_INFRA** (deploy).

### 7. No authentication — every endpoint is open

Reserved and ready (`owner_id` on every run, `for_user()` through the
repositories, Supabase env names agreed), built by the parallel session.
Until it lands, anyone who can reach the ALB can upload and execute Python.

- Where: **this repo**, parallel auth session.

### 8. Validation executes user Python with admin database credentials

Documented everywhere it is implemented, as a speed bump and not a sandbox.
Acceptable for a club behind auth; not beyond it. Needs a scoped database role
(the infra text already says `MARKET_DATA_USER must be a READ-ONLY role`;
today it is `admin`) and process isolation.

- Where: **CAIR ops** (restricted role), **this repo** (isolation, deferred).

### 9. Smaller items

| Item | Where |
| --- | --- |
| Container health-check example uses `curl`; `python:3.11-slim` has none. Leave the container check off (default) or use `python -c "urllib..."`. | infra |
| Dockerfile pins Python 3.11; development and tests run 3.12. Both have wheels for the engine pins — align to 3.12 to test what ships. | this repo |
| `.dockerignore` did not exclude `data/` or `.strategy_store/` — a warm parquet cache would have been baked into the image. **Fixed here.** | this repo |
| Trade metrics count closed round trips only; a buy-and-hold strategy shows `totalTrades: 0` beside a real return. Mark open positions to the final bar. | this repo |
| `mode: "fast"` is unusable (`int(PORTFOLIO_ID)`); only event mode runs. | this repo |

---

## What was checked and found fine

- **Engine drift: none.** `MQSMaster` has moved 21 commits on `main` since the
  vendored `31d9570`, but nothing under `src/backtest/` or `src/portfolios/`
  changed. Only `MQSDBConnector` (signature tidy-up) and `schemaDefinitions`
  (date index, pruner) moved; the engine's `execute_query` contract that our
  adapter mirrors is unchanged. The `idx_market_data_date` index the upstream
  renamed already exists on the live server.
- **Frontend contract: matches.** The run form's `backtestRunRequestSchema`
  (`name, strategyKey, startDate, endDate, initialCapital, mode?, params?`) is
  exactly what `POST /backtests` reads; `coverageResponseSchema`
  (`tickers[{ticker, firstBar, lastBar}], start, end, missing`) is exactly what
  `/market-data/coverage` returns. The FE has an uncommitted
  `run-backtest-dialog.tsx` in progress — untouched, as agreed.
- **Frontend still calls `/live/portfolios*` and `/live/system/*`** — served as
  sample data, labelled as such; out of scope.
- **Every FE endpoint has a backend route.** Nothing the client asks for 404s.
