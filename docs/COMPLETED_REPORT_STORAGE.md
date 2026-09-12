# Completed report storage

Only successful backtests create rows in `app.backtest_reports`. There is no
status column, placeholder row, or partial report. Failed and cancelled runs
never enter saved history. The JSON document also excludes `status`,
`progressPct`, and `errorMessage`.

The table has exactly seven columns:

```sql
CREATE TABLE app.backtest_reports (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_key TEXT NOT NULL REFERENCES app.strategies(key) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    results JSONB NOT NULL CHECK (
        jsonb_typeof(results) = 'object' AND NOT (results ? 'status')
    )
);
CREATE INDEX ix_backtest_reports_owner_created
    ON app.backtest_reports (owner_id, created_at DESC, id);
```

`created_at` retains submission time. `version = 1` describes the report JSON
format. Strategy source/config hashes, storage key, engine version, resolved
configuration, and execution settings live in `results.reportMetadata`.
The report contains all daily chart observations, metrics, trades, open
positions, and submitted parameters. Source packages remain in the strategy
store. Exact replay of changing market data is not promised.

## HTTP behavior

- `POST /api/backtests` returns the existing 202 job response, held in memory.
- `GET /api/backtests/{id}` polls temporary progress/errors or reads saved JSON.
- `GET /api/backtests` lists saved successful user reports only. Validation
  reports are available by ID but excluded from ordinary history. A legacy
  status filter for any value other than completed returns an empty page.
- Detail, charts, exports, deletion, and strategy uploads resolve the current
  `X-User-Id` identity, or `TEMPORARY_USER_ID` when the header is absent. Every
  report lookup/deletion includes the resolved owner's UUID.
- The frontend still receives `status`, progress, and error fields for its
  existing polling contract. Completed status is synthesized from the presence
  of a saved report; it is not stored in PostgreSQL.
- `DELETE` cancels a transient job or deletes a saved report. Cancellation and
  the final report write are serialized, so a cancellation accepted before
  persistence prevents that write. After completion, deletion removes the row.

The worker computes in a spawned process and returns a complete, validated
report. The API's completion callback inserts the entire document in one
transaction and only then reports successful completion. History queries
project the card fields in SQL instead of transferring each complete report.

## Temporary job state

Use one API process/instance. Its multiprocessing workers share progress and
cancellation through local IPC. Increasing API replicas requires a separate
shared job coordinator; database report storage itself remains shared.

An API restart discards unfinished jobs and their temporary errors. They are
not automatically resumed. An unknown job with no saved report returns 404;
the frontend should stop polling and invite a new submission. Terminal
temporary state expires after one hour. Completed reports survive restarts
and do not depend on `.artifacts` files. `X-User-Id` is still the temporary
identity mechanism, not verified authentication.

## Temporary account before auth

The local backend `.env` sets
`TEMPORARY_USER_ID=4510522a-07e1-4dba-98c3-e83bbee3cfe3`, an existing row in
`public.user_creds`. Requests without an identity use this account for
submission, strategy validation, history, chart data, exports, and deletion.
The frontend development account uses the same UUID. An explicit header still
selects its validated user; invalid explicit identities are never replaced by
the fallback. No new user is created per request or per run.

The fallback is off by default in `.env.example`. Remove `TEMPORARY_USER_ID`
and replace the header identity dependency with verified auth when sign-in is
implemented. Existing saved reports retain their owner.

Verification: 81 focused tests passed. A real headerless submission named
`Temporary user verification` completed and saved report
`c5a33cef-aa78-4694-bc98-ebb6334f8a43` under this UUID, with five daily equity
observations and no stored status. The auto-reloading local API has loaded
the fallback configuration.

## Existing data and rollout

Schema initialization adds the new report table and a `validation_job_id`
pointer on the existing strategy registry. Legacy `app.backtest_runs` and its
result tables, and `public.backtest_runs`, are retained without modification
of their records. They are no longer active report writers/readers. Existing
legacy worker helpers remain for migration and historical tests.

Copy successful legacy reports with the provided dry-run command:

```powershell
.\venv\Scripts\python.exe scripts/migrate_completed_reports.py --assign-unowned-to 4510522a-07e1-4dba-98c3-e83bbee3cfe3
```

Add `--apply` to perform the copy after inspecting its counts. Existing owned
rows keep their owner; the supplied UUID is used only for unowned legacy rows.
Failed/unfinished rows and summary-only public rows are not imported. Missing
report observations and conflicting IDs fail explicitly. Re-running the same
migration is idempotent. Old rows are retained as the rollback source.

Roll out with one API instance and drain its old jobs before replacement.
Restart the API to use the new writer, then copy existing history. Rolling back
the code restores legacy history; new JSON reports remain in their table and
become visible again when the new code is restored. No migration drops data.

## Verification

Live follow-up on 2026-09-11: the local API's auto-reloader had already created
the table and loaded the new code. After a successful dry run, all 26 completed
legacy reports were copied using the ownership mapping above. All 27 legacy
rows remain unchanged; the current test account lists 21 user reports. Live
history, detail, equity, and JSON export checks passed, and none of the 26
saved JSON documents contains a status field. The frontend now supplies its
development identity on strategy upload requests as well as backtest requests;
20 targeted frontend tests and TypeScript checking passed. A hosted API
deployment has not been verified.

`tests/unit/test_completed_reports.py` proves no connection/write on queueing,
progress, failure, crash, cancellation, or rejected dispatch, and verifies
ownership, repeated runs, failed persistence, document shape, and expiry.
The guarded disposable PostgreSQL proof in `test_ci_pipeline.py` executes a
real uploaded strategy in spawned workers and checks the JSON report, legacy
table non-use, numerical results, exports, cross-user access, deletion, and
retrieval after an API restart.

Local verification on 2026-09-11: 706 tests passed in the suite excluding
`db`/`ci_db`; the guarded PostgreSQL module passed all 23 checks. A separate
disposable-database migration check verified dry runs, owned and unowned
copies, repeat execution, exclusion of failures, and preservation of legacy
records. These checks did not modify the live database. The repository's
layering check still detects the pre-existing direct engine import in
`src/api/routes/market_data.py`; that unrelated route was not changed here.
