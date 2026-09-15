# Starter reports

After a successful Cognito-authenticated API request, an account with no real
report history receives two simulated examples for the enabled built-in
`portfolio_1` and `portfolio_2` strategies. Their names start with `Example:` and
end with `(simulated)`. Their charts and trades are generated illustrations:
they do not use FMP prices or execute the strategy engine.

## API contract

- `GET /api/backtests` returns real completed user reports. Examples are excluded
  from its rows, pagination totals, and searches, so clients using this feed for
  performance calculations cannot accidentally include them.
- `GET /api/backtests/examples` returns only the authenticated owner's examples,
  using the same paginated response and `search`, `status`, `strategyId`, `page`,
  and `pageSize` filters. It does not expose other users' examples.
- Existing detail, equity, export, and delete routes work with an example's ID
  and retain their ownership checks. An example's detail metadata explicitly
  identifies its simulated source and `purpose: "example"`.
- Example CSV exports include `reportType` and `reportName` columns so their
  simulated origin remains visible outside the application. Real-report CSV
  columns are unchanged.

The frontend can use the examples endpoint for a separate examples section.
This backend release does not add that frontend section. Keep examples out of
real strategy performance comparisons and retain their simulated labels when
displaying or exporting them.

## Persistence and concurrency

The user's identity remains keyed by verified Cognito issuer and subject in
`app.users`; it is created on the first authenticated API request, not during
Cognito signup. This change does not synchronize email or display name claims.

The additive `starter_reports_seeded_at` column records a completed onboarding
decision. Existing owners with real history are marked without adding examples.
If the required built-ins are unavailable, onboarding is deferred.

The example inserts and marker share one transaction. A refreshed row lock
serializes concurrent first requests, and deterministic owner-specific IDs
prevent duplicate identities. Deleting examples leaves the marker intact, so
later requests do not recreate them. Examples remain ordinary owner-scoped
JSONB documents in `app.backtest_reports`, with a distinct example purpose.

## Database inspection

Use schema-qualified names in pgAdmin:

```sql
SELECT id, issuer, subject, email, starter_reports_seeded_at
FROM app.users;

SELECT id, owner_id, name, results -> 'reportMetadata' ->> 'purpose' AS purpose
FROM app.backtest_reports
ORDER BY created_at DESC;
```

`public.user_creds` and `public.backtest_runs` are separate legacy tables.
Connect to the database configured for the deployed backend before running
these queries; a separate MQSMaster RDS instance does not automatically contain
the application's `app` schema.

## Release and rollback

CI tests the API contract and concurrent onboarding against disposable
PostgreSQL. Production startup adds the nullable marker column idempotently;
no existing reports or identities are deleted. For rollback, retain the marker
column and example documents while restoring the previous backend image.
The previous default history query already filters for user-purpose reports,
so newly created example-purpose reports stay out of that feed.
