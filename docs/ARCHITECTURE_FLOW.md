# Architecture Flow

Where a request enters, what it touches, and where it ends up — traced against
the code, with `file:line` references you can click.

Every diagram below is Mermaid, so GitHub renders it inline.

**Read this first:** the one fact that shapes the whole design is that a
backtest is **minutes of single-core CPU work, not a request**. Everything
downstream — the 202 response, the process pool, the polling, the progress
column — follows from that.

- [1. System overview](#1-system-overview)
- [2. Startup](#2-startup)
- [3. Submitting a run](#3-submitting-a-run)
- [4. Worker execution](#4-worker-execution)
- [5. Reading results](#5-reading-results)
- [6. Uploading a strategy](#6-uploading-a-strategy)
- [7. Layering rules](#7-layering-rules)
- [8. File map](#8-file-map)

---

## 1. System overview

```mermaid
flowchart TB
    FE["<b>Frontend</b><br/>separate repo · React + Vite<br/>calls /api via Vite proxy"]

    subgraph API["FastAPI process — stays warm and responsive"]
        ROUTES["<b>src/api/routes/</b><br/>HTTP only: parse, delegate, serialize"]
        SERVICES["<b>src/services/</b><br/>business rules, validation"]
        REPOS["<b>src/repositories/</b><br/>the only place SQL lives"]
        POOL["<b>src/workers/job_manager.py</b><br/>ProcessPoolExecutor"]
    end

    subgraph WORKER["Worker process — burns a core for minutes"]
        RUNJOB["<b>src/workers/run_job.py</b><br/>claim · execute · persist"]
        ENGINE["<b>engine/</b><br/>vendored backtest engine"]
    end

    subgraph PG["PostgreSQL — one instance, two roles"]
        MD[("public.market_data<br/><i>READ ONLY</i>")]
        APP[("app.*<br/><i>this app owns</i>")]
    end

    DISK["<b>.artifacts/&lt;run_id&gt;/</b><br/>engine CSVs"]
    STORE["<b>.strategy_store/</b><br/>uploaded strategies<br/><i>S3-shaped</i>"]

    FE -->|"POST /api/backtests"| ROUTES
    FE -.->|"poll GET /api/backtests/id"| ROUTES
    ROUTES --> SERVICES --> REPOS --> APP
    SERVICES -->|"submit run_id"| POOL
    POOL ==>|"spawn"| RUNJOB
    RUNJOB --> ENGINE
    ENGINE --> MD
    ENGINE --> DISK
    RUNJOB --> APP
    RUNJOB -.->|"user strategies"| STORE

    classDef fe fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef api fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef worker fill:#fff3e0,stroke:#e65100,color:#e65100
    classDef data fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    class FE fe
    class ROUTES,SERVICES,REPOS,POOL api
    class RUNJOB,ENGINE worker
    class MD,APP,DISK,STORE data
```

The **double arrow** is the process boundary. It is the whole reason the API
can answer other requests while a backtest runs.

---

## 2. Startup

`uvicorn server:app` → everything below happens once, in order, before the
first request is served.

```mermaid
flowchart TD
    START(["uvicorn server:app"]) --> SERVER["<b>server.py</b><br/>FastAPI app<br/>lifespan=application_lifespan"]
    SERVER --> LIFE["<b>src/workers/job_manager.py:239</b><br/>application_lifespan"]

    LIFE --> SCHEMA["<b>src/db/init.py</b> · ensure_schema<br/>CREATE SCHEMA app + create_all<br/><i>models in src/models/</i>"]
    SCHEMA --> RECON["<b>src/workers/reconciler.py</b><br/>'running' + stale heartbeat → 'failed'<br/><i>still beating = another live worker, left alone</i>"]
    RECON --> REQUEUE["<b>job_manager.py:228</b> · _requeue_orphans<br/>runs left 'queued' → resubmitted<br/><i>never claimed, so safe</i>"]
    REQUEUE --> POOL["<b>job_manager.py:173</b> · start_job_manager<br/>ProcessPoolExecutor"]
    POOL --> ROUTER["<b>src/api/router.py</b><br/>routes mounted at /api"]
    ROUTER --> READY(["ready"])

    classDef boot fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef fix fill:#fff3e0,stroke:#e65100,color:#e65100
    class START,SERVER,LIFE,SCHEMA,POOL,ROUTER,READY boot
    class RECON,REQUEUE fix
```

Two things are deliberate and easy to break:

**Order matters.** The reconciler runs *after* schema creation — it corrects
rows in tables that must already exist.

**The pool is built in the lifespan and nowhere else.** On Windows a process
pool *spawns* workers by re-importing the module tree. A pool built at import
time would rebuild itself inside every worker it created; under `--reload` the
first file save turns that into a fork bomb. A lifespan runs once per real
server process and never inside a spawned worker.

**The reconciler judges by heartbeat, not by status.** `status='running'` only
says a worker claimed the row; whether that worker is alive is what
`heartbeat_at` answers. Each worker beats every few seconds on a thread
independent of engine progress — progress callbacks stop for minutes during a
cold `market_data` load, so they cannot serve as liveness. A boot fails only
rows whose beat is stale, which is what lets two API instances, a rolling
deploy, or two test modules in one process coexist without killing each
other's runs.

---

## 3. Submitting a run

```mermaid
flowchart TD
    REQ(["POST /api/backtests"]) --> ROUTE["<b>src/api/routes/backtests.py:52</b><br/>create_backtest"]
    ROUTE --> SVC["<b>src/services/backtests.py:570</b><br/>submit_backtest_run"]

    SVC --> LOAD["<b>:345</b> _load_runnable_strategy"]
    LOAD --> SREPO["<b>src/repositories/strategies.py</b><br/>SELECT app.strategies"]
    SREPO --> ENABLED{"exists and<br/>enabled?"}
    ENABLED -->|no| E422

    ENABLED -->|yes| VAL["<b>:390-:544</b> validation<br/>name · window · capital · mode · params"]
    VAL --> VOK{"all valid?"}
    VOK -->|no| E422["<b>422</b><br/>one sentence in 'detail'<br/><i>unknown param names the key</i>"]

    VOK -->|yes| SYM["<b>:558</b> _symbol_for<br/>single ticker, else MULTI"]
    SYM --> CREATE["<b>:288</b> create_backtest_run"]
    CREATE --> RREPO["<b>src/repositories/runs.py:149</b><br/>INSERT app.backtest_runs<br/>status = queued"]
    RREPO --> DISP["<b>:604</b> _dispatch"]
    DISP --> SUBMIT{"pool accepts?"}
    SUBMIT -->|yes| OK(["<b>202</b> + BacktestSummary<br/><i>the row, not the result</i>"])
    SUBMIT -->|no| FAILED["<b>:626</b> _fail_undispatched<br/>mark run failed"]
    FAILED --> OK

    classDef http fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef svc fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef db fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    classDef err fill:#ffebee,stroke:#c62828,color:#b71c1c
    class REQ,ROUTE,OK http
    class SVC,LOAD,VAL,SYM,CREATE,DISP svc
    class SREPO,RREPO db
    class E422,FAILED err
```

**Why 202 carries the full summary** rather than just an id: the client drops
it straight into its list cache, so the new run appears immediately instead of
after a refetch.

**A 202 is not a promise of success** — only that the run exists and is the
client's to watch. If the pool refuses the job outright, the payload comes back
already marked `failed`, because a run nothing will ever pick up must not look
like a run waiting its turn.

---

## 4. Worker execution

This is where the actual backtest happens, in a separate OS process.

```mermaid
flowchart TD
    SPAWN(["ProcessPoolExecutor worker"]) --> RJ["<b>src/workers/run_job.py:119</b><br/>run_job"]
    RJ --> CLAIM["<b>:216</b> _claim<br/>UPDATE ... WHERE status='queued'"]
    CLAIM --> GOT{"rows<br/>affected?"}
    GOT -->|0| NOOP(["return — another worker has it<br/><i>idempotent under redelivery</i>"])

    GOT -->|1| CTX["<b>:333</b> _load_context<br/>read run + strategy"]
    CTX --> KIND{"strategy<br/>kind?"}
    KIND -->|builtin| CP["<b>:376</b> _resolve_class_path<br/>engine.strategies.*"]
    KIND -->|user| MAT["<b>:400</b> _materialize_user_strategy<br/>src/integrations/strategy_store.py<br/>→ temp dir in engine layout"]

    CP --> BUILD["<b>:451</b> _build_request<br/>RunRequest + on_progress + should_cancel<br/><i>:241 _RunHeartbeat, throttled ~1/s</i>"]
    MAT --> BUILD
    BUILD --> RS["<b>engine/run_single.py:176</b><br/>run_single"]

    subgraph ENG["engine/ — knows nothing about HTTP or SQLAlchemy"]
        RS --> LOADC["<b>:46</b> load_strategy_class"]
        LOADC --> BE["<b>engine/core/backtest_engine.py</b>"]
        BE --> RUNNER["<b>engine/core/runner.py</b><br/>_run_event_loop"]
        RUNNER --> FETCH["<b>engine/core/utils.py</b><br/>fetch_historical_data"]
        FETCH --> CACHE["<b>engine/data/cache.py</b><br/>data/backfill_cache/*.parquet"]
        FETCH --> ADAPTER["<b>engine/data/db_adapter.py</b><br/>public.market_data · READ ONLY"]
        RUNNER --> ONDATA["<b>engine/strategies/portfolio_N/strategy.py</b><br/>OnData"]
        ONDATA --> IND["<b>engine/indicators/</b>"]
        ONDATA --> EXEC["<b>engine/core/executor.py</b><br/>BacktestExecutor → fills"]
        RUNNER --> REPORT["<b>engine/analytics/reporting.py</b><br/>metrics + CSVs"]
    end

    RUNNER -.->|"on_progress"| PROG[("UPDATE progress_pct")]
    RUNNER -.->|"should_cancel"| CANCELQ[("SELECT cancel_requested")]
    REPORT --> ART["<b>.artifacts/&lt;run_id&gt;/</b>"]

    RS --> RESULT{"outcome"}
    RESULT -->|"no bars"| NMD["<b>NoMarketData</b><br/>names tickers + window"]
    RESULT -->|"raised"| FAIL["<b>:701</b> _fail<br/>status=failed + error_message"]
    RESULT -->|"ok"| PERSIST["<b>:476</b> _persist_success"]
    NMD --> FAIL

    PERSIST --> PAIR["<b>src/services/trade_pairing.py</b><br/>pair_fills · FIFO round trips"]
    PAIR --> TX["<b>one transaction</b><br/>INSERT run_metrics · run_equity_points<br/>· run_trades<br/>UPDATE run → completed"]
    TX --> VAL{"purpose ==<br/>validation?"}
    VAL -->|yes| OUTCOME["<b>:641</b> apply_validation_outcome<br/>strategy → active / failed_validation"]
    VAL -->|no| DONE(["done"])
    OUTCOME --> DONE
    FAIL --> DONE

    classDef worker fill:#fff3e0,stroke:#e65100,color:#e65100
    classDef eng fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef db fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    classDef err fill:#ffebee,stroke:#c62828,color:#b71c1c
    class SPAWN,RJ,CLAIM,CTX,CP,MAT,BUILD,PERSIST,PAIR,TX,OUTCOME,DONE,NOOP worker
    class RS,LOADC,BE,RUNNER,FETCH,CACHE,ADAPTER,ONDATA,IND,EXEC,REPORT eng
    class PROG,CANCELQ,ART db
    class NMD,FAIL err
```

Four correctness rules live in this diagram, each guarding a specific failure:

| Rule | Without it |
| --- | --- |
| Claim with `WHERE status='queued'` | A redelivered job runs twice concurrently |
| Progress writes throttled to ~1/sec | A long run hammers the database for cosmetic updates |
| Equity curve downsampled to daily before insert | ~98k rows stored for a chart that plots trading days |
| Empty bars raise `NoMarketData`, exceptions re-raise | A crashed run reports **success with no results** |

That last one is the important one. Upstream in MQSMaster, `run()` catches
per-portfolio exceptions, logs them, and returns an empty trade log — correct
for a batch CLI where a human reads the console, fatal for a hosted API.

---

## 5. Reading results

The client polls while `status` is non-terminal.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant R as routes/backtests.py
    participant S as services/backtests.py
    participant P as repositories/runs.py
    participant W as worker process
    participant DB as app.backtest_runs

    C->>R: POST /api/backtests
    R->>S: submit_backtest_run
    S->>P: create_run (status=queued)
    S-->>W: submit run_id
    R-->>C: 202 + BacktestSummary

    W->>DB: claim → status=running
    loop while running
        C->>R: GET /api/backtests/{id}
        R->>S: get_backtest :231
        S->>P: get_run :123
        P-->>S: run + metrics + equity + trades
        S->>S: to_detail :193
        S-->>C: status, progressPct
        W->>DB: UPDATE progress_pct
    end

    W->>DB: metrics + equity + trades, status=completed
    C->>R: GET /api/backtests/{id}
    R-->>C: completed + full results
```

`BacktestDetail` carries two fields the list row does not: **`progressPct`**
(drives the progress bar) and **`errorMessage`** (the only place a failed — or
cancelled — run can say why it stopped). Both are additive; the client's Zod
schema ignores keys it does not declare, so they shipped safely ahead of the UI.

---

## 6. Uploading a strategy

A strategy is proven compatible **by running a real backtest on it**. The
validation run goes through the pipeline in §4 unchanged — same worker, same
progress, same error reporting.

```mermaid
flowchart TD
    UP(["POST /api/strategies  (JSON source)<br/>POST /api/strategies/upload  (multipart .py)"]) --> SR["<b>src/api/routes/strategies.py</b><br/>file → UTF-8 text, then one shared path"]
    SR --> SS["<b>src/services/strategies.py</b>"]
    SS --> SV["<b>src/services/strategy_validation.py</b>"]

    SV --> SCAN["<b>:176</b> scan_source<br/>AST import allowlist<br/>⚠ SPEED BUMP, NOT A SANDBOX"]
    SCAN --> SOK{"clean?"}
    SOK -->|no| R422["<b>422</b> naming the offending line<br/><i>nothing stored</i>"]

    SOK -->|yes| ONE["<b>:248</b> _sole_strategy_class_name<br/>exactly one BasePortfolio subclass"]
    ONE --> OOK{"exactly one?"}
    OOK -->|no| R422

    OOK -->|yes| CFG["<b>:314</b> build_config"]
    CFG --> STORE["<b>:337</b> store_strategy_source<br/><b>src/integrations/strategy_store.py</b>"]
    STORE --> LAYOUT[".strategy_store/strategies/&lt;key&gt;/<br/>strategy.py + config.json<br/><i>mirrors engine/strategies/ layout</i>"]
    LAYOUT --> ROW["INSERT app.strategies<br/>kind=user · status=validating · enabled=false"]
    ROW --> WIN["<b>:368</b> validation_window"]
    WIN --> SV2["<b>:389</b> start_validation<br/>run with purpose='validation'"]
    SV2 --> PIPE["<b>the pipeline in §4</b>"]
    SV2 --> TO["<b>:436</b> _schedule_timeout"]
    SV2 --> RESP(["<b>201</b> status='draft'<br/><b>validationRunId</b> to poll"])
    RESP -.->|"GET /strategies/{key}<br/>validationState · validationRunId"| OUT

    PIPE --> OUT{"run outcome"}
    OUT -->|completed| ACT["strategy → <b>active</b> + enabled<br/><i>now selectable; reruns via §3</i>"]
    OUT -->|failed| BAD["strategy → <b>failed_validation</b><br/>error kept on the run row"]

    classDef http fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef svc fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef warn fill:#fff8e1,stroke:#f57f17,color:#e65100
    classDef err fill:#ffebee,stroke:#c62828,color:#b71c1c
    classDef good fill:#e0f2f1,stroke:#00695c,color:#004d40
    class UP,SR,RESP http
    class SS,SV,ONE,CFG,STORE,LAYOUT,ROW,WIN,SV2,TO,PIPE svc
    class SCAN warn
    class R422,BAD err
    class ACT good
```

**Watching an upload.** The catalogue (`GET /strategies`) shows only enabled
rows, so a validating or failed upload is invisible there by design. The
client watches it through `GET /strategies/{key}` — `validationState` carries
the real lifecycle and `validationRunId` is the run to open for a progress bar
or, on failure, the reason — and `POST /strategies/check` (or `/upload/check`)
answers "would this run here?" in milliseconds before anything is submitted.

**The layout mirroring is load-bearing, not cosmetic.** `BasePortfolio` finds
its `config.json` by looking next to its `strategy.py`
(`inspect.getfile` sibling lookup), so a materialized upload must land in that
exact shape for the engine to load it with no special-casing.

> **Security.** Validation executes user-supplied Python in a worker process
> holding credentials to the production trading database. The AST allowlist and
> the timeout are speed bumps a determined author walks around — they are not
> isolation. Real containment (container, no network egress, a scoped database
> role) is required before this is exposed beyond the club.

---

## 7. Layering rules

Enforced by review, and checkable with grep.

```mermaid
flowchart LR
    A["<b>src/api/routes/</b><br/>HTTP only"] --> B["<b>src/services/</b><br/>business rules"]
    B --> C["<b>src/repositories/</b><br/>all SQL"]
    C --> D[("app.*")]
    B -.-> E["<b>src/integrations/</b><br/>external systems"]
    F["<b>src/workers/</b><br/>sync DB only"] --> G["<b>engine/</b>"]
    G --> H["<b>engine/data/db_adapter.py</b><br/><i>engine's only outward seam</i>"]
    H --> I[("public.market_data")]

    classDef api fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef svc fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef worker fill:#fff3e0,stroke:#e65100,color:#e65100
    classDef eng fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef db fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    class A api
    class B,E svc
    class C,F worker
    class G,H eng
    class D,I db
```

| Invariant | Check |
| --- | --- |
| `engine/` imports nothing from `src/` | `grep -rn "^\s*\(from\|import\)\s\+src\." engine/` → 0 |
| No web framework or ORM in `engine/` | `grep -rln "fastapi\|sqlalchemy" engine/` → 0 |
| Routes never touch the ORM | `grep -rln "sqlalchemy" src/api/` → 0 |
| Routes never import the engine | `grep -rn "^\s*from engine" src/api/` → 0 |

The engine staying free of `src` is what keeps it runnable standalone — and what
would make swapping it for a pip-installed package a one-line change.

---

## 8. File map

Ordered the way a request travels.

| Stage | File | Role |
| --- | --- | --- |
| Boot | [server.py](../server.py) | ASGI app, CORS, lifespan wiring |
| Boot | [src/workers/job_manager.py](../src/workers/job_manager.py) | Pool, `application_lifespan`, orphan requeue |
| Boot | [src/db/init.py](../src/db/init.py) | `CREATE SCHEMA app` + `create_all` |
| Boot | [src/workers/reconciler.py](../src/workers/reconciler.py) | Interrupted runs → `failed` |
| Config | [src/core/config.py](../src/core/config.py) | The only module reading the environment |
| HTTP | [src/api/router.py](../src/api/router.py) | Mounts everything under `/api` |
| HTTP | [src/api/routes/backtests.py](../src/api/routes/backtests.py) | List, submit, detail, delete/cancel |
| HTTP | [src/api/routes/strategies.py](../src/api/routes/strategies.py) | Catalogue, upload |
| HTTP | [src/api/routes/portfolios.py](../src/api/routes/portfolios.py) | `/live/*` — **sample data**, out of scope |
| HTTP | [src/api/routes/system.py](../src/api/routes/system.py) | `/live/*` — **sample data**, out of scope |
| Contract | [src/schemas/](../src/schemas) | Pydantic models; camelCase mirrors the FE Zod types |
| Logic | [src/services/backtests.py](../src/services/backtests.py) | Validation, submission, detail assembly |
| Logic | [src/services/strategy_validation.py](../src/services/strategy_validation.py) | AST scan, store, validation run |
| Logic | [src/services/trade_pairing.py](../src/services/trade_pairing.py) | Fills → FIFO round trips |
| Data | [src/repositories/](../src/repositories) | All SQL; `for_user()` seam awaits auth |
| Data | [src/models/](../src/models) | ORM models for the `app` schema |
| External | [src/integrations/strategy_store.py](../src/integrations/strategy_store.py) | S3-shaped store, local backend |
| Execution | [src/workers/run_job.py](../src/workers/run_job.py) | Claim, run, persist, validation outcome |
| Engine | [engine/run_single.py](../engine/run_single.py) | The seam: one run in, `RunResult` out |
| Engine | [engine/core/](../engine/core) | Simulation kernel, event loop, executor |
| Engine | [engine/analytics/](../engine/analytics) | Metrics, reporting, vectorized mode |
| Engine | [engine/data/](../engine/data) | Parquet cache + database adapter |
| Engine | [engine/strategies/](../engine/strategies) | Strategy classes + their `config.json` |
| Engine | [engine/indicators/](../engine/indicators) | Loaded dynamically by name |
| Engine | [engine/contracts/](../engine/contracts) | `RunRequest`, `RunResult`, errors |

### Where things land on disk

```
.artifacts/<run_id>/          engine CSVs for one run          (gitignored)
data/backfill_cache/*.parquet market data cache, one per ticker (gitignored)
.strategy_store/strategies/   uploaded strategy source          (gitignored)
```

---

## Known limitations

| Limitation | Consequence |
| --- | --- |
| `mode: "fast"` is unusable | Only `event` runs. `_build_fast_portfolio_stub` does `int(PORTFOLIO_ID)`, which raises on non-numeric ids |
| OMS not vendored | All four vendored strategies have it disabled, so no behavioural difference today |
| `TLT` bars end 2025-11-07 | Caps the window for any strategy whose universe includes it |
| No authentication | `owner_id` exists on every run and `for_user()` is wired through the repositories, but nothing enforces ownership yet |
| `/live/*` is sample data | The live trading views are not backed by the trading tables |
