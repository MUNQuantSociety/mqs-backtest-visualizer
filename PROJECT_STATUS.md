# Project status and handoff

## Completed-only reports (2026-09-11)

The current checkout stores successful backtests in the seven-column
`app.backtest_reports` table. It has no status column; failed, cancelled,
queued, and running jobs are never inserted. Job state uses temporary IPC.
See [the storage contract](docs/COMPLETED_REPORT_STORAGE.md) for migration and
the single-API-process/restart limits. Older run tables are retained for
non-destructive migration. The local auto-reloading API has loaded this code and
created the table in the configured database. The 26 completed legacy reports
were copied and verified; all 27 legacy rows remain intact. Existing owners
were retained and unowned reports assigned to the agreed test account. Its
history now lists 21 user runs. Live detail, equity and JSON export checks pass.
The frontend upload identity header was also updated and tested. This does not
claim a deployment to the hosted API.

Until auth is implemented, local requests without `X-User-Id` use the existing
test account configured by `TEMPORARY_USER_ID` in backend `.env`. This is the
same UUID already configured in the frontend; saved reports remain owned by it.

Status recorded: **2026-09-10**. Implementation baseline: backend `dev`
at `a04e0f8`, frontend integration at `2debac8`, infrastructure integration
at `93f7390`. This document records evidence and remaining work; it is not
a claim that every frontend panel is real or that production is deployed.

## Bottom line

### Follow-up: S3 built-ins and visible logging (2026-09-10)

`portfolio_1` and `portfolio_2` source/config were published and byte-verified
under the existing development S3 prefix. Their registry IDs and run history
were retained. In S3 mode the catalogue checks registered package availability;
workers download the selected package instead of importing the local built-in.
Missing packages/outages no longer silently substitute demo strategies/coverage.
Run-specific controls do not rewrite the shared package.

INFO terminal output now covers HTTP arrival/response, catalogue/package checks,
coverage and controls, queue/worker startup, S3 downloads, engine stages and
percentage progress, report persistence and failures. Requests carry a response
`X-Request-ID`; worker logs include run IDs. See the
[S3/logging runbook](docs/S3_STRATEGY_FLOW.md) for commands and stage meanings.

Live checks used a temporary no-reload backend on 8123, database-backed prices,
and the actual frontend Vite proxy on 5174 (fixtures disabled). This isolated
verification from concurrent edits/reloads in the main development server; it
is not a production deployment or a new authenticated browser-click verification.

| Strategy / run | Inputs | Verified result |
| --- | --- | --- |
| `portfolio_1` / `1a5ec2ee-1492-4d90-9777-cf62ca82f7c7` | CRWV/NBIS; 2025-10-15 through 2025-11-07; capital 200,000 | Completed; 15 daily points; no fills; final equity 200,000. No synthetic trades were added. |
| `portfolio_2` / `2cb659d7-093c-4dfd-93f3-c0331b047619` | Same tickers/dates; capital 175,000; submitted through Vite proxy | Completed; 15 daily points; 4 FIFO trade lots; final equity 186,108.420325. |

Both reports identify `backend=s3`, their storage keys and source hashes matching
the published local source. Both retain the chosen benchmark universe and
capital. Report reads and CSV export through the frontend proxy returned 200.
The earlier run `a328bd69-a449-4740-bc2e-1315504c140f` failed on a worker
KeyboardInterrupt during development reload and is retained as a failed run.

Verification during this follow-up: the collected offline backend suite passed
561 tests (94 deselected); after adding HTTP logging, the focused S3/logging
suite passed 18 tests. These are snapshots, not certification of concurrent
market-data work added by another session. Frontend type checking passed.
The final logging-only check passed all 6 tests, including visible nonzero
slippage/commission precision and HTTP request correlation/redaction.
The real-mode strategy/coverage API tests passed in the initial focused run;
the frontend form suite still has timing failures during this shared-checkout
session (loading/default-date updates). The final rerun also timed out starting
a Vitest worker, so a fully green frontend regression suite is not claimed here.
Temporary verification servers on 8123/5174 were stopped after the successful
runs; existing user development servers were left in place.
Existing release blockers below remain unchanged.

### Prior implementation baseline

The core student backtesting workflow is implemented and was verified with
the real frontend, PostgreSQL market data and private S3 strategy storage:

**Submit Python source/file -> compatibility check -> validation backtest ->
active strategy -> select and run -> persist report -> display charts/export.**

The four previously unfinished workstreams are addressed: chart/report data,
S3 storage, CI/CD configuration and setup documentation. CI is active and green
at the baseline commit. Production deployment is deliberately disabled pending
database configuration, authentication/authorization and execution isolation.
There is no separate dev deployment, as requested.

## Completed implementation

| Area | Delivered behavior | Main implementation / reference |
| --- | --- | --- |
| Backend structure | Python API, schemas, services, repositories, database models, workers and vendored engine have separate responsibilities. Frontend and Terraform remain in separate repositories. | [Architecture](docs/ARCHITECTURE_FLOW.md), `src/`, `engine/` |
| Engine integration | MQSMaster backtesting code is adapted behind a single-run interface. Event mode executes the selected strategy against configured market data. This is not a promise of parity with MQSMaster live order slicing or every vector adapter. | `engine/run_single.py`, `engine/contracts/`, `engine/core/`, `engine/VENDORED_FROM` |
| PostgreSQL | Real market-data reads, application strategy registry, run state, metrics, equity points and trades persist in PostgreSQL. Coverage checks validate the requested universe/date bounds. | `src/db/`, `src/models/`, `src/repositories/`, `engine/data/` |
| Run API | Submit, list, inspect, cancel/delete and export backtests. A submission returns a queued run; results arrive asynchronously. | `src/api/routes/backtests.py`, `src/services/backtests.py` |
| Background execution | Spawned process pool, atomic run claiming, progress writes, worker heartbeat, cooperative cancellation and stale-run reconciliation. | `src/workers/` |
| Strategy submission | Accept source JSON or multipart Python files, enforce size/contract checks, provide line-level compatibility feedback, queue a validation backtest and activate successful submissions. | `src/api/routes/strategies.py`, `src/services/strategy_validation/` |
| Student template | The backend serves a starter Python template and a preflight check endpoint. The template is tested against the implemented strategy contract. | `src/services/strategy_validation/template.py`, `engine/strategies/user_loader.py` |
| Strategy storage | Local and S3 implementations share the same interface. S3 clients are lazy; keys are guarded against traversal; missing objects and storage errors have explicit handling. | `src/integrations/strategy_store.py` |
| Existing-source migration | Local-to-S3 migration defaults to dry-run, requires stopped workers for execution, verifies content and does not delete local originals. | `scripts/migrate_strategy_store_s3.py` |
| Browser connection | The frontend's real Run Backtest form calls FastAPI through its `/api` proxy, polls progress and displays stored results with fixture mode disabled. Existing login was used without bypassing or rewriting it. | [Verification](docs/VERIFICATION.md), frontend integration `2debac8` |
| Execution controls | Dates, capital, universe, slippage in basis points and per-share commission reach the engine. Changed universes receive equal weights; the same ticker set preserves configured weights. | `src/services/run_controls.py`, [report contract](docs/REPORT_CONTRACT.md#execution-controls) |
| Documentation | Installation, environment configuration, seeding, server/log commands, frontend connection, tests, report semantics, architecture and deployment gates are documented. | [README](README.md), [report contract](docs/REPORT_CONTRACT.md), [CI/CD](docs/CI_CD.md) |

### Upload lifecycle: important distinction

The preflight check reads source without executing or storing it. Actual
submission stages source/config in the selected store and creates a draft
strategy before the worker runs validation. A successful validation makes it
active and selectable. Failed validation does not make a strategy available
for normal reruns. S3 persistence is therefore not itself proof of validation.

Passing compatibility and validation means the strategy conforms and ran for
the validation window. It does not prove that it is secure, profitable, or
correct for every date range.

### Reports and frontend charts

New completed reports provide:

- Daily equity and configured-universe buy-and-hold benchmark observations.
- Return, CAGR, Sharpe, Sortino, drawdown, volatility and closed-trade metrics.
- FIFO trade lots, entry/exit information, realized P&L and allocated fees.
- Separate final open-position marks and unrealized P&L where available.
- Calculation, benchmark coverage and execution-cost metadata.
- Explicit reasons for undefined metrics instead of treating every placeholder
  zero as a measured value.

The frontend uses these to populate equity/benchmark, monthly returns, daily
P&L, rolling Sharpe/volatility, return distributions, beta scatter, drawdowns,
trade plots and the tearsheet. Return correlation was excluded as requested.
Performance, Risk, Trades and Tearsheet tabs were inspected on a real run.

Canonical downloads are available through
`GET /api/backtests/{id}/exports/{filename}` for `equity.csv`, `trades.csv`,
`metrics.csv` and `report.json`. They come from persisted database results,
not arbitrary worker paths. The frontend reads equivalent API JSON; CSV files
are verification/download artifacts, not a separate frontend fixture feed.

Charts still need sufficient observations: short runs can legitimately leave
rolling charts empty, and missing benchmark prices remain unavailable.
Historical reports are not recalculated automatically; rerun the strategy to
obtain the new contract. See the [report contract](docs/REPORT_CONTRACT.md) for
metric definitions, baseline treatment, fee accounting and missing-data rules.

### S3 and infrastructure

The dedicated private strategy bucket, task role and GitHub deployment role
were provisioned through the separate infrastructure repository's
`terraform/environments/Backtest_Visualizer_Integrations/` root.

- Bucket: `mqs-backtest-visualizer-strategies-855603407903-us-east-2`.
- Public access blocked; HTTPS-only policy, SSE-S3 encryption and versioning.
- Local verification uses `development/`; production task access is restricted
  to `production/strategies/*`.
- Upload, retrieval, materialization and engine loading were verified. A fresh
  template upload passed validation and had source/config confirmed in S3.
- Existing source was copied and verified without deleting its local original.
- Existing ECS services/network were not replaced or rolled out by this work.

Do not add AWS keys, database credentials, `.env`, generated artifacts or
virtual environments to Git. Detailed resource and operational notes are in
[CI/CD](docs/CI_CD.md) and [verification](docs/VERIFICATION.md).

### CI/CD and source control

- `dev` pushes and PRs targeting `dev`/`main` run unit tests, a disposable
  PostgreSQL pipeline test and an image smoke test, with aggregate check `test`.
- Main ruleset `22692640` is active: PR required, up-to-date passing `test`,
  no bypass actors, no force pushes or branch deletion.
- Main's release workflow reuses CI and deploys to the existing production ECS
  service through GitHub OIDC and the protected `production` environment.
- The release checks preserve task configuration, pin the image digest and
  verify the intended running task revision rather than just service stability.
- **`PRODUCTION_DEPLOY_ENABLED=false`** was rechecked on 2026-09-10. No production
  deployment was performed. Do not enable it just because tests pass.
- [Backend PR #3](https://github.com/MUNQuantSociety/mqs-backtest-visualizer/pull/3)
  and [infrastructure PR #19](https://github.com/MUNQuantSociety/MQS_AWS_INFRA/pull/19)
  were open at this snapshot. No main merge is part of this handoff.
- Backend implementation is pushed to `dev`; frontend integration is also
  pushed to its `dev`. Separate user-owned frontend `package.json` and
  `package-lock.json` edits remain uncommitted and were intentionally untouched.

## Verification evidence

These are measured results from the implementation verification, not tests
rerun by merely writing this document.

| Check | Result / evidence |
| --- | --- |
| Latest backend unit CI at `a04e0f8` | **545 passed, 1 expected skip, 93 deselected**; image and aggregate checks also passed. [Dev CI run](https://github.com/MUNQuantSociety/mqs-backtest-visualizer/actions/runs/34413434071) |
| Disposable PostgreSQL pipeline | **23 passed** in hosted CI; exercises upload, validation, activation, spawned workers, repeatable reruns, cost accounting, database persistence and all four exports. |
| PR merge-ref checks at the same implementation SHA | All green. [PR CI run](https://github.com/MUNQuantSociety/mqs-backtest-visualizer/actions/runs/34413437214) |
| Frontend integration | **99 tests passed**, typecheck/build passed; edited feature paths passed lint. |
| Actual browser run | `9234faf5-751e-4a64-a246-7245068557f7`: 69 equity and 69 benchmark points, 3 FIFO lots, 1 closed lot, fees 2.64, final equity 98756.088715 from initial capital 100000. |
| Fresh S3-backed submission | Validation run `21ff1639-51da-44b9-8d03-105b1cb624c3` completed; its strategy became active with both objects confirmed in S3. |
| Infrastructure | Integration Terraform root validated and applied; dedicated resources created, narrowly scoped IAM follow-up applied, no production service rollout. |

The browser result's requested window was 2026-03-02 through 2026-07-15.
It submitted 5 bps slippage and 0.005 commission per share. Its 69 dates are
observed data, not a claim of complete exchange-calendar coverage. Full details
and local artifact locations are in [verification](docs/VERIFICATION.md).
Existing dependency/build warnings are recorded there; passing checks are not
a guarantee of zero bugs or production readiness.

## Remaining work: release blockers

| Priority | Task and responsible area | Completion condition |
| --- | --- | --- |
| P0 | **Integrate authentication and enforce ownership** — coordinate the separate auth work with this backend. Frontend login alone is not API authorization; repository ownership filtering is not enforced. | API validates identity; strategy/run access, exports and mutations enforce ownership; cross-user and unauthenticated access tests pass. |
| P0 | **Isolate uploaded Python execution** — backend/security/infrastructure. Current AST checks and spawned workers are not a sandbox, and workers hold database/storage access. | Uploaded code cannot access application secrets or unrelated files/network; CPU, memory and hard runtime limits are enforced, including non-cooperative infinite loops. Test these boundaries before public exposure. |
| P0 | **Secure the production database path** — database/infrastructure owners. The last live inspection reported PostgreSQL TLS off. | Provide and verify a working TLS connection, intended database grants and ECS network reachability. Do not silently downgrade production SSL. |
| P0 | **Configure and verify the existing ECS task** — infrastructure/backend owners. The inspected task lacked database environment/secret references and task-role wiring. | Configure secure database references, appropriate roles and health checks; verify actual database/S3 access from the service. Deployment preflight must pass without weakening its guards. |
| P1, after P0 | **Release through main and verify production** — repository/release owners. | Review and merge the relevant PRs, verify CI for the final commit, enable the release flag only after prerequisites, approve production deployment and verify exact revision/digest plus upload -> validation -> rerun -> report/export on the deployed service. |

Authentication integration, execution isolation and database/ECS preparation
can progress in parallel with clear ownership. Production release depends on
all three. This handoff does not authorize bypassing these gates or merging main.

## Other gaps and intentional limits

These are not silently included in the completed backtest work:

- **Dashboard news and indicators:** `/api/news` and `/api/indicators` are not
  implemented. The frontend market panels explicitly fall back to fixtures on
  missing endpoints, even with backtest fixture mode disabled. Decide whether
  to implement real feeds or clearly hide/label those panels in a separate task.
- **Live trading pages:** `/api/live/*`, including portfolio and system/log
  views, return sample data. They are outside the backtest-only integration.
- **Signal/sentiment overrides:** nonempty signal overrides and enabled
  sentiment gating are rejected; corresponding run controls are disabled.
- **Fast mode:** limited to supported adapters, approximates strategy behavior,
  emits no executed fills/final position marks, and rejects nonzero per-share
  commission. Use event mode for the verified student workflow.
- **Data completeness:** coverage bounds do not prove continuous interior
  bars. Benchmark metadata reports missing/delayed entries and last observations;
  it does not invent prices to fill gaps.
- **Engine scope:** portfolios 4-8 and live OMS TWAP/VWAP slicing were not brought
  into this product. Do not claim full parity with the entire MQSMaster stack.
- **Schema evolution:** `create_all` creates missing objects, not a general
  migration history. Plan explicit migrations before incompatible schema changes.
- **Progress transport:** polling is implemented; SSE/WebSockets are deferred,
  not required to make the current workflow function.

## Local server shutdown and resuming work

On 2026-09-10, at the user's request:

- The API reported **0 queued and 0 running backtests** before shutdown.
- Stopped the backend on `127.0.0.1:8000`, frontend listeners on both
  `127.0.0.1:5173` and `[::1]:5173`, and a leftover backend worker.
- Verified no listeners remained on checked project/test ports
  `8000`, `8001`, `8123`, `5173`, `5174`, `4173`, or `55432`.
- Unrelated system/Codex services, remote PostgreSQL and AWS resources were
  not stopped. Source, stored runs, S3 objects and local artifacts were retained.

This records the shutdown, not a guarantee that another session cannot start a
server later. Use [README quick start](README.md#quick-start) to resume the
backend with terminal logs, and [frontend connection instructions](README.md#connect-the-frontend-to-the-real-api)
to start the matching frontend. Do not expose uploads publicly until the release
blockers above are cleared.
