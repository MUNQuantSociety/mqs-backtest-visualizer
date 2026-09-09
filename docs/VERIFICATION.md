# Integration verification — 2026-09-09

This records measured local behavior, not a production-deployment claim.
The matching frontend changes are in `MUNQuantSociety/Backtest_Visualiser_FE`;
infrastructure resources are in `MQS_AWS_INFRA`, under
`terraform/environments/Backtest_Visualizer_Integrations`.

## Browser to real results

The existing frontend login was used without changing or bypassing authentication.
With fixture mode disabled, the actual **Run backtest** form submitted a saved
user strategy through Vite's `/api` proxy to FastAPI. The request was accepted,
the worker materialized its source/config from private S3, queried market data,
and persisted a completed report. Performance, Risk, Trades and Tearsheet tabs
were inspected in the browser, including the benchmark and rolling charts.

| Evidence | Observed value |
| --- | --- |
| Run | `9234faf5-751e-4a64-a246-7245068557f7` |
| Requested window | 2026-03-02 through 2026-07-15 |
| Initial / final equity | 100000 / 98756.088715 |
| Daily equity / benchmark points | 69 / 69 (only observed dates; missing days are not fabricated) |
| FIFO lots / closed lots | 3 / 1 |
| Allocated fees | 2.64 |
| Costs submitted by the form | 5 bps slippage; 0.005 per share commission |

CSV and JSON exports were downloaded through the same frontend proxy into
`.artifacts/9234faf5-751e-4a64-a246-7245068557f7/report/`. These files are ignored
local evidence. Durable exports are served from persisted database results by
`GET /api/backtests/{id}/exports/{equity.csv,trades.csv,metrics.csv,report.json}`.
The frontend reads the equivalent JSON report, not a separate mock dataset.

The first browser attempt exposed reserved form controls being treated as
strategy parameters. The fix explicitly separates/validates universe and costs.
Unknown parameters remain errors. Unsupported signal overrides and sentiment
gating are disabled in the form and rejected by the backend when enabled.

## Fresh upload and S3

A fresh submission of the API's starter template, through the frontend proxy,
passed compatibility checking and a real validation backtest:

- Strategy: `user-s3-upload-verification-2026-09-09-50017689`.
- Validation run: `21ff1639-51da-44b9-8d03-105b1cb624c3`, completed.
- Registry state: `active`; both `strategy.py` and `config.json` confirmed in S3.

The dedicated bucket is
`mqs-backtest-visualizer-strategies-855603407903-us-east-2`. Verification/local
work uses `development/`; production IAM is restricted to `production/strategies/*`.
It has public access blocked, HTTPS-only policy, SSE-S3 encryption and versioning.
Existing local source was copied and byte-verified without deleting the original.

The live storage verifier passed put/get/list/materialize/engine-load/delete
checks. Representative warm calls were about 90 ms for a get, 154–164 ms for a
put and 275 ms for materialization. These are local observations, not an SLA.
Only its own temporary test objects were deleted; version history remains.

## Automated proof

- Backend offline suite: **538 passed, 1 expected opt-in skip, 93 deselected**.
  The database and storage boundaries were isolated from live services.
- Disposable PostgreSQL pipeline: **16 passed**. Fresh upload, validation,
  activation and two identical browser-shaped reruns use the real spawned worker
  and engine. Tests assert 5 bps fills, positive fees, exact repeatability and
  final equity = capital + realized P&L + open-position P&L − fees (within a cent).
  Database rows and all four exports are checked too.
- Frontend: **99 passed**, production build/typecheck passed. Edited feature
  paths passed ESLint after correcting a test-only feature-boundary import.
- Terraform integration root: validated; dedicated resources applied. The
  tag-permission follow-up changed one policy, with no resource destruction.

The local suite emits an existing Starlette/httpx deprecation warning. The
frontend build reports the existing large demo-data chunk warning.

## Release gates and outstanding operations

Main's active `main-pr-and-ci` ruleset requires a PR and the `test` check, with
an up-to-date branch and no bypass actors. The GitHub `production` environment
allows only main and requires reviewer approval. Dev is **CI only**, not a
separate deployment. See [CI/CD](CI_CD.md) for the workflows and release checks.

`PRODUCTION_DEPLOY_ENABLED=false` is intentional. No ECS rollout was performed.
Before enabling production:

1. Database operations must provide a working TLS connection. The inspected
   CAIR server reported `ssl=off`; do not silently downgrade production TLS.
2. Operations must configure the existing ECS task's database secret references
   and verify service health. Its inspected revision had no database environment
   or secret references. The deployment workflow supplies the dedicated S3 role
   and configuration only after its preflight passes.
3. Integrate the separately owned authentication/authorization work, and isolate
   untrusted Python execution before exposing student uploads publicly. The
   current AST checks and worker process are not a security sandbox.

These are release blockers, not reasons to present a skipped deployment as
successful. Existing historical reports are not recalculated automatically;
rerun strategies to generate the new report contract.
