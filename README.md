# MQS Backtest Visualizer — Backend

A hosted, multi-user web application that lets students run quantitative trading
backtests and explore the results visually — without touching the trading repo,
a terminal, or a database.

This repository is the **Python backend**: the REST API, the authentication
layer, the database, the job queue, and the isolated backtest engine lifted out
of [`MQSMaster`](https://github.com/MUNQuantSociety/MQSMaster).

**This repo is backend only.** Two sibling repositories cover the rest, and
neither has any presence here:

| Repository | Owns |
| --- | --- |
| Frontend | The entire user interface. Built in parallel; nothing in here renders UI. |
| Infrastructure | Terraform, cloud resources, environment provisioning. |

Deployment is driven from `.github/workflows/` — there is no `deploy/`,
`docker/`, or Terraform directory in this repository by design.

> **Status:** scaffold. Folder structure, environment template, and docs only —
> no implementation code has been written yet.

---

## Table of contents

- [What this project is](#what-this-project-is)
- [Relationship to MQSMaster](#relationship-to-mqsmaster)
- [The constraint that shapes the architecture](#the-constraint-that-shapes-the-architecture)
- [Architecture at a glance](#architecture-at-a-glance)
- [Folder structure](#folder-structure)
- [Environment configuration](#environment-configuration)
- [Getting started](#getting-started)
- [Conventions](#conventions)
- [Roadmap](#roadmap)

---

## What this project is

MQS runs a quantitative trading system with nine portfolio strategies and a
mature backtest engine. That engine is currently only reachable by cloning the
trading repo, configuring a database, editing constants in a Python file, and
running a CLI. That is a hard door for a student who wants to learn how a
momentum strategy behaves in a drawdown.

This project puts a web front door on it. The expectation is that a member logs
in, picks a strategy, fills in a form (date range, starting capital, tickers,
cost model), clicks run, watches a live progress bar, and then reads an equity
curve, a drawdown chart, a metrics table, a trade log, and a Monte Carlo fan
chart — and can compare that run against someone else's.

**In scope for this repository**

| Area | What it means here |
| --- | --- |
| REST API | FastAPI, versioned under `/api/v1`, OpenAPI schema published for the frontend |
| Authentication | Supabase issues JWTs; this backend verifies them against a public JWKS and provisions users on first request |
| Authorization | Per-user private runs with explicit sharing; owner scoping enforced in the repository layer |
| Persistence | PostgreSQL — users, strategies, runs, metrics, artifacts, shares |
| Job execution | A queue and a worker process that runs backtests off the request path |
| Engine | The isolated backtest engine, its strategies, indicators, and analytics |
| Artifacts | Run output (CSV/parquet) written to local disk in dev, object storage in production |

**Explicitly out of scope**

- Frontend code — separate repository.
- Infrastructure as code — separate repository. This repo builds and ships;
  it does not provision.
- Live trading. This platform reads market data and simulates. It must never
  write to `positions_book`, `cash_equity_book`, or `trade_execution_logs`.
- User-authored strategy code. Phase 2. The structure below leaves room for it
  (see [Roadmap](#roadmap)) but nothing here executes untrusted Python.

---

## Relationship to MQSMaster

The engine originates in `MQSMaster/src/backtest/` and its dependencies
(`src/portfolios/`, indicators, the market-data layer). The goal is to **isolate
it** so this platform depends on a stable, versioned engine rather than on the
whole trading system.

Two viable ways to consume it, both compatible with the `engine/` layout below:

1. **Vendored** — the engine source lives in `engine/` in this repo, adapted so
   it takes an injected data provider and artifact sink instead of reaching into
   Postgres and local disk itself.
2. **Packaged** — `pip install git+https://github.com/MUNQuantSociety/MQSMaster.git@<tag>`
   and `engine/` holds only the thin adapter that maps platform concepts (a run
   request) onto engine calls.

Whichever is chosen, one rule holds: **pin a tag, never track a branch.** A
silent engine change must not alter historical backtest results. Every run
records the engine version that produced it.

Engine work that must land before the platform can consume it cleanly — artifact
sink, structured metrics instead of CSV-only, real error propagation, a progress
callback, and an injectable data provider — is specified in
`MQSMaster/docs/platform/BACKTEST_PLATFORM_PLAN.md`.

---

## The constraint that shapes the architecture

**A backtest is a long CPU-bound job, not an HTTP request.**

Event-mode runs step timestamp by timestamp at the strategy's poll interval
across the requested window *plus* a lookback prefix. A multi-month run over a
dozen tickers is minutes to tens of minutes of single-core work.

Three consequences run through every design decision in this repo:

1. There is **no synchronous endpoint that returns results**. `POST /runs`
   validates, enqueues, and returns `202 Accepted` with a `run_id`.
2. The **API and the worker are separate processes** — the API stays small and
   warm, workers scale with queue depth.
3. **Progress is reported out of band**, or a user stares at a spinner for
   twenty minutes wondering if it broke.

---

## Architecture at a glance

```
      Frontend (separate repo)
              │
              ▼
      ┌───────────────┐        enqueue        ┌───────────────┐
      │   FastAPI     │ ────────────────────► │  Job queue    │
      │   src/api     │                       │  Redis / SQS  │
      └───────┬───────┘                       └───────┬───────┘
              │                                       │ consume
              │ read/write                            ▼
              │                               ┌───────────────┐
              │                               │    Worker     │
              │                               │  src/workers  │
              │                               └───────┬───────┘
              │                                       │ invoke
              ▼                                       ▼
      ┌───────────────┐                       ┌───────────────┐
      │  PostgreSQL   │ ◄──── metrics ─────── │    Engine     │
      │  app schema   │                       │   engine/     │
      └───────────────┘                       └───────┬───────┘
                                                      │ read bars
      ┌───────────────┐                               │
      │   Artifacts   │ ◄──── CSV/parquet ────────────┤
      │  local / S3   │                               ▼
      └───────────────┘                       ┌───────────────┐
                                              │  market_data  │
                                              │  (read-only)  │
                                              └───────────────┘
```

Requests flow through four layers, and the boundary between them is deliberate:

```
route  →  service  →  repository  →  database
```

- **Routes** are thin: authenticate, validate, delegate, serialize. No SQL, no
  business rules.
- **Services** hold the rules — parameter validation against a strategy schema,
  deduplication, enqueueing, run lifecycle.
- **Repositories** own data access *and* owner scoping. Because application data
  lives in a plain PostgreSQL instance rather than Supabase, there is no Row
  Level Security to fall back on — every query goes through a user-scoped
  accessor so a forgotten check in a route cannot leak another user's runs.

---

## Folder structure

```
mqs-backtest-visualizer/
│
├── server.py                   ASGI entrypoint — builds the app, mounts
│                               middleware and routers. `uvicorn server:app`
│
├── src/                        The backend application package
│   ├── api/                    HTTP layer only. No business logic lives here.
│   │   ├── dependencies/       Reusable FastAPI dependencies — current user,
│   │   │                       DB session, pagination, rate limiting
│   │   └── v1/                 Version 1 of the public API
│   │       └── routes/         One module per resource: health, auth,
│   │                           strategies, runs, artifacts, shares
│   │
│   ├── core/                   Cross-cutting infrastructure
│   │                           config (typed settings from .env), security
│   │                           (JWT/JWKS verification), logging, exception
│   │                           handlers, constants
│   │
│   ├── db/                     Database plumbing — engine, session factory,
│   │                           declarative base, seed helpers
│   │
│   ├── models/                 SQLAlchemy ORM models: the `app` schema tables
│   │                           (users, strategies, backtest_runs, run_metrics,
│   │                           run_artifacts, run_shares, audit_log)
│   │
│   ├── schemas/                Pydantic models — request bodies, response
│   │                           payloads, validation. The API contract, and the
│   │                           source of the OpenAPI schema the frontend
│   │                           generates its client from.
│   │
│   ├── repositories/           Data access. Owner scoping is enforced HERE,
│   │                           not in routes. One repository per aggregate.
│   │
│   ├── services/               Business logic — submitting a run, validating
│   │                           params against a strategy's schema, dedup by
│   │                           params hash, cancellation, sharing, metrics
│   │
│   ├── workers/                Queue consumer that executes backtests off the
│   │                           request path: claim, run, upload, record.
│   │                           Also the visibility heartbeat and the reconciler
│   │                           that fails runs orphaned by a killed task.
│   │
│   ├── integrations/           Adapters to everything external — the backtest
│   │                           engine, object storage, the queue, the identity
│   │                           provider. Swappable; keeps vendor SDKs out of
│   │                           services.
│   │
│   └── utils/                  Small shared helpers with no domain knowledge
│                               (time, hashing, series downsampling)
│
├── engine/                     The isolated backtest engine, lifted from
│   │                           MQSMaster/src/backtest. Deliberately standalone:
│   │                           it knows nothing about HTTP, users, or auth, and
│   │                           can be unit-tested without a running API.
│   ├── core/                   Simulation kernel — engine, event-loop runner,
│   │                           execution/fill simulation, cost & slippage model
│   ├── contracts/              The interfaces the platform depends on: run
│   │                           request, run result, progress callback, market
│   │                           data provider, artifact sink. This is the seam
│   │                           that keeps the engine swappable.
│   ├── strategies/             Portfolio strategy classes and their configs
│   ├── indicators/             Technical indicators used by strategies
│   ├── analytics/              Metrics, reporting, vectorized/fast mode,
│   │                           Monte Carlo, cross-validation
│   └── data/                   Market data providers and caching — the only
│                               place the engine touches a data source
│
├── tests/
│   ├── unit/                   Fast, no I/O — services, schemas, engine math
│   ├── integration/            Real database, real queue
│   ├── e2e/                    Full request → queue → worker → result path
│   └── fixtures/               Shared test data and sample market bars
│
├── scripts/                    Operational one-offs — seed the strategy
│                               registry, backfill data, reconcile stuck runs
│
├── docs/                       Architecture decisions, API notes, runbooks
│
├── .github/workflows/          CI/CD — lint, test, build, deploy. The only
│                               deployment surface in this repo; there is no
│                               deploy/ or docker/ directory, and Terraform
│                               lives in the infrastructure repo.
│
├── .env.example                Environment template — copy to .env
├── .gitignore
└── README.md
```

### Why this shape

- **`src/` and `engine/` are separate top-level packages.** The engine is the
  thing being isolated; burying it inside the web application would recreate the
  coupling this project exists to remove. `engine/` has no FastAPI import
  anywhere in it.
- **The backend package is `src/`, not `app/`.** `app/` is the Next.js App
  Router convention and belongs to the frontend repo; using it here would be
  actively confusing across the two codebases. `src/` also matches MQSMaster's
  existing src-layout, so the two Python repos read the same way.
- **`engine/contracts/` is load-bearing.** If the engine receives its data
  provider and artifact sink rather than constructing them, then running a
  strategy against a pre-materialized parquet file — with no database
  credentials anywhere near it — becomes a swap instead of a rewrite. That is
  what makes Phase 2 (user-authored strategies in a sandbox) achievable.
- **`repositories/` exists as its own layer** for one reason: it is where owner
  scoping is enforced. Without Row Level Security, that has to be somewhere
  structural.
- **`workers/` lives inside `src/`, not beside it**, because it shares config,
  models, and repositories with the API. Same build, different entrypoint.
- **No `deploy/` or `docker/`.** Terraform and cloud resources are the
  infrastructure repo's job, and build/release is driven from
  `.github/workflows/`. A container definition, if one is ever needed here, sits
  at the repo root rather than in a directory of its own.

### Legacy placeholders

`auth/` and `route/` predate this layout and are superseded by `src/core/`
(JWT verification, security) and `src/api/v1/routes/`. Both are empty. Remove
them when convenient:

```bash
git rm -r --cached auth route && rm -rf auth route
```

---

## Environment configuration

All configuration comes from environment variables. `.env.example` is the
annotated template — copy it to `.env` and fill in real values:

```bash
cp .env.example .env
```

It covers application settings, the read-only market data connection,
Supabase/JWT verification, per-user limits, and external data API keys. It is
deliberately minimal — variables get added as the features that need them land,
so the template always reflects what the code actually reads.

Two rules worth stating up front:

- **Nothing reads `os.environ` directly.** Configuration is parsed once into a
  typed settings object in `src/core/` and injected. Typos fail at startup, not
  at 2 a.m. in a worker.
- **The market data connection is read-only.** Grant `SELECT` and nothing else.
  The platform simulates; it does not trade.

`.env` and `venv/` are gitignored; `.env.example` is tracked, so the template
arrives with every clone. It holds placeholders only — never paste a real
credential into it.

---

## API endpoints

Everything is mounted under `/api`, which is what the frontend's
`VITE_API_BASE_URL` resolves to. Vite proxies that prefix to this server in
development, so the browser sees a same-origin URL and CORS is never exercised.

Responses are **camelCase** and the client parses each one with Zod. A renamed
or snake_case key is a hard failure in the browser, not a cosmetic difference —
`tests/unit/test_api_contract.py` guards the shapes.

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness. No database, no engine, no I/O. |
| `GET` | `/api/backtests` | Paginated. Filters: `search`, `status`, `strategyId`, `page`, `pageSize` |
| `GET` | `/api/backtests/{id}` | Detail — metrics, equity curve, trades |
| `DELETE` | `/api/backtests/{id}` | `204`, or `404` if unknown |
| `GET` | `/api/strategies` | Catalogue with per-strategy run aggregates |
| `POST` | `/api/strategies` | Accepts source, stores a draft. Never executes it. |
| `GET` | `/api/live/portfolios` | Live portfolio list |
| `GET` | `/api/live/portfolios/{id}` | Detail — config, positions |
| `GET` | `/api/live/portfolios/{id}/equity` | `days` |
| `GET` | `/api/live/portfolios/{id}/composition` | `days`. Column-wise series. |
| `GET` | `/api/live/portfolios/{id}/executions` | `ticker`, `page`, `pageSize` |
| `GET` | `/api/live/portfolios/{id}/correlations` | Full square matrix |
| `GET` | `/api/live/system/status` | Per-service health |
| `GET` | `/api/live/system/logs` | `size`. Tail, not archive. |

Interactive docs at `http://localhost:8000/docs` once the server is running.

**These endpoints serve generated sample data, not real results.** `src/services/sample_data.py`
stands in for the database so the frontend can develop against real HTTP —
real status codes, real serialisation, real error paths — before any table
exists. Routes call it through functions, so swapping in `src/repositories/`
later touches nothing else. Data is deterministic from a fixed seed: a chart
that changed on every refresh would make a backend bug indistinguishable from
noise.

---

## Getting started

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Then start the frontend in its own repo (`npm run dev`). It proxies `/api` to
port 8000, so both must be running to see data.

Once the rest of the implementation begins:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill it in
```

The intended entrypoints, for orientation:

| Purpose | Command |
| --- | --- |
| API server (dev) | `uvicorn server:app --reload` |
| Background worker | `python -m src.workers` |
| Tests | `pytest` |
| Fast tests only | `pytest tests/unit` |

---

## Conventions

- **Python 3.10+.** The engine inherits pins from the trading repo — pandas
  `2.2.2`, numpy `<=1.26.4`. Do not assume newer pandas APIs in `engine/`.
- **All timestamps are `America/New_York`.** Market data is stored and
  normalized in that zone end to end. Store timezone-aware `timestamptz`.
- **Async in the API, sync in the engine.** The API and repositories are async
  (SQLAlchemy 2.0 async). The engine is CPU-bound synchronous code and runs in
  the worker process, never inside an event loop.
- **Every run records its engine version.** A backtest result is uninterpretable
  without knowing the code that produced it.
- **Failures must surface.** A run that crashed reports `failed` with a message
  a student can act on. Silently returning empty results is worse than an error.
- **Schema changes are versioned and reviewed**, never applied by hand against a
  deployed database. This repo does not carry a migrations directory — schema
  management lives outside it, and `src/models/` must be kept in step with
  whatever is deployed.

---

## Roadmap

| Milestone | Deliverable |
| --- | --- |
| **M0** | Engine isolation — artifact sink, structured metrics, error propagation, progress callback, injectable data provider, tagged release |
| **M1** | Infrastructure — database, queue, storage, CI |
| **M2** | Backend — auth + JWKS verification, strategy registry, run CRUD, worker, artifact upload |
| **M3** | Frontend integration — login, new-run wizard, run list, run detail with charts |
| **M4** | Live progress streaming, cancellation, run comparison, sharing |
| **M5** | Phase 2 — user-authored strategies in a sandboxed worker pool with no database credentials and no network egress |

Full design rationale, database schema, endpoint table, and infrastructure plan:
`MQSMaster/docs/platform/BACKTEST_PLATFORM_PLAN.md`.
