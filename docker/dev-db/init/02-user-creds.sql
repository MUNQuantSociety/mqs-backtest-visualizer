-- public.user_creds — the second table this application READS but never owns.
--
-- Until real authentication lands, every request identifies its caller with an
-- `X-User-Id` header, and src/api/dependencies/current_user.py rejects the
-- request with 401 unless that id is a row in this table. Without it the
-- container is not merely missing a feature: POST /api/backtests answers
-- "Send X-User-Id with a public.user_creds id." and no backtest can be
-- submitted at all.
--
-- In production the table is owned outside this app (see BACKEND_PLAN.md); the
-- visualiser only ever SELECTs from it, and src/repositories/user_creds.py
-- reads exactly the three columns below. The real table has more — reproduce a
-- column here only when this application starts reading it.
--
-- The two rows are the dummy users the test-suite already hard-codes
-- (tests/integration/test_public_run_list.py, tests/unit/test_api_contract.py),
-- so a container seeded from this file can run those tests unchanged. Keep the
-- UUIDs stable for that reason.
--
-- Runs only while the data volume is empty; `docker compose down -v` to re-run.

CREATE TABLE IF NOT EXISTS public.user_creds (
    id           UUID PRIMARY KEY,
    email        TEXT NOT NULL,
    display_name TEXT
);

INSERT INTO public.user_creds (id, email, display_name) VALUES
    ('4510522a-07e1-4dba-98c3-e83bbee3cfe3', 'alice@example.com', 'Alice'),
    ('bb961a0d-52af-4303-9fdf-1ccc941e3c07', 'bob@example.com',   'Bob')
ON CONFLICT (id) DO NOTHING;
