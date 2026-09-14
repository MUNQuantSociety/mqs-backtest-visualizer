# The Docker development database

Everything in this application ultimately asks PostgreSQL a question. The
default answer to "how do I run this locally?" has been *point `.env` at the
university warehouse* — which needs VPN access, puts every developer's
experiments on the same host the live trading system reads, and makes a
destructive mistake a shared incident rather than a personal one. It is also
simply unavailable when the network is down.

This is the alternative: a throwaway PostgreSQL container with the one table
this application reads, filled with synthetic bars. It costs about a minute to
set up and can be destroyed and rebuilt with one command.

**What it is not.** The bars are invented (a seeded random walk), so a run
against this database proves the *plumbing* works — the engine loads data,
metrics compute, reports render, the frontend draws a chart. It proves nothing
about whether a strategy is any good. Keep performance conclusions on the real
warehouse.

---

## What you get

| | |
|---|---|
| PostgreSQL 17 | `localhost:5433`, database `mqsdb`, user `mqs` — same major version as the MQS instance |
| `public.market_data` | Created empty at first boot, then filled by the seeder |
| `app.*` | Created by `seed_strategies.py`, or by the API on first boot |
| Data | 14 tickers x 365 trading days of hourly bars (~41k rows) |

Port **5433**, not 5432, so this cannot collide with a PostgreSQL you already
run for something else.

---

## Prerequisites

* Docker Desktop running (`docker compose version` should print a version).
* The Python virtualenv from [Quick start](README.md#quick-start), because the
  seeder and the verification scripts are Python.
* A `.env` file. If you do not have one: `cp .env.example .env`.

---

## Step 1 — start the database

```bash
docker compose up -d db
```

The first boot creates the volume, runs `docker/dev-db/init/01-market-data.sql`,
and takes a few seconds longer than later ones. Wait for it to report healthy
rather than racing it:

```bash
docker compose ps db
# STATUS should read "Up (healthy)", not "Up (health: starting)"
```

The healthcheck is `pg_isready -U mqs -d mqsdb`, which passes only once the
named database accepts queries — a bare `pg_isready` would go green while the
schema script was still running.

## Step 2 — point `.env` at it

Edit `.env` and replace the `POSTGRES_*` block with these six lines. Keep your
real warehouse values somewhere you can paste back — commenting them out in
place works well.

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
POSTGRES_DB=mqsdb
POSTGRES_USER=mqs
POSTGRES_PASSWORD=mqs_dev_password
POSTGRES_SSLMODE=disable
```

`sslmode=disable` is the honest value here: the container serves plaintext on
a loopback port. The `prefer` in `.env.example` is written for the remote
instance, and the comment there about `require` being rejected describes *that*
server, not this one. Both drivers this application uses accept `disable` —
`src/core/config.py` already translates the libpq vocabulary into asyncpg's
`ssl` argument.

You also need one line outside that block:

```dotenv
MARKET_DATA_SOURCE=database
```

Without it the engine never opens a connection. `MARKET_DATA_SOURCE` defaults
to `fmp`, and the FMP adapter checks its API key before anything else, so every
run fails with `FMPUnavailable: FMP_API_KEY is missing` — an error that says
nothing about the database and sends you looking in the wrong place. The
container can hold a perfectly seeded table and still serve none of it.

Read `.env.example` before you set this: it calls `database` the *legacy* price
source, and FMP is where the application is going. This whole document
describes developing against that legacy path — which is the point, since it is
what works offline and without a key, but it is not the production data source.

One `.env` is enough for both halves of the application. The API reads these
names through `src/core/config.py`; the engine reads the *same* names directly
in `engine/data/db_adapter.py`. That is deliberate, and it means there is no
second place to change and forget.

> **If you run the API in a container instead** (`docker compose up --build`),
> do not use the values above. Inside the compose network the database is
> `db:5432`, not `localhost:5433` — `compose.yaml` already sets that on the
> `server` service, so you do not have to.

## Step 3 — fill it with bars

The table starts empty, and an empty `market_data` does not fail loudly: you
get empty coverage, a validation window that cannot be built, and a backtest
that "succeeds" over nothing. Seed it before you do anything else.

```bash
venv/bin/python scripts/seed_dev_db.py
```

That writes 365 trading days for every ticker any seeded strategy trades. It is
idempotent — re-running extends coverage rather than duplicating rows — so it
is safe to run again whenever you want more history:

```bash
venv/bin/python scripts/seed_dev_db.py --days 730
venv/bin/python scripts/seed_dev_db.py --tickers AAPL,MSFT --days 90
```

### The parquet cache can hide an empty table

`engine/core/utils.py` caches bars per ticker in `data/backfill_cache/*.parquet`
and reads them before querying Postgres. A warm cache therefore satisfies a run
that the database could not: with `market_data` truncated to zero rows, an
event-mode backtest still completes with a full equity curve, served entirely
from those files. The `NoMarketData` guard never fires because nothing ever
asked the database.

This does not affect a fresh clone — no cache, correct error. It bites when you
switch an existing checkout from the warehouse to this container: runs keep
succeeding on stale warehouse prices and nothing says so. Clear the cache when
you change data sources.

```bash
rm -rf data/backfill_cache
```

Fast mode does not use this cache and fails correctly either way, so a run that
succeeds in event mode and fails in fast mode with `NoMarketData` is this
situation.

The script refuses to run when `POSTGRES_HOST` is not local. `public.market_data`
is owned by the live trading system and this is the only thing in the repository
that inserts into it; the guard is what stops a forgotten `.env` from writing
synthetic prices into the warehouse.

## Step 4 — create the `app` schema and the strategies

```bash
venv/bin/python scripts/seed_strategies.py
```

Bars alone are not enough: the API serves runs of *strategies*, and a fresh
database has none. This creates the `app` schema and upserts the four built-in
strategies, so there is something to run.

The API would create the schema by itself on startup (`src/db/init.py` builds
it from the models — which is why the init SQL deliberately does not, one
source of truth rather than two). Doing it here instead means the verification
below passes on a database the API has never touched, and it is the step that
gives you the strategies either way.

### You also need a user

Every request identifies its caller with an `X-User-Id` header, and
`src/api/dependencies/current_user.py` returns 401 unless that id is a row in
`public.user_creds`. Without one, `POST /api/backtests` answers *"Send
X-User-Id with a public.user_creds id."* and no backtest can be submitted at
all — however well `market_data` is seeded.

`docker/dev-db/init/02-user-creds.sql` creates that table and inserts two dummy
users, Alice and Bob, using the same UUIDs the test suite hard-codes:

| User | `X-User-Id` |
| --- | --- |
| Alice | `4510522a-07e1-4dba-98c3-e83bbee3cfe3` |
| Bob | `bb961a0d-52af-4303-9fdf-1ccc941e3c07` |

Like every file in `init/`, it runs only while the data volume is empty. A
container you created before this file existed will not have the table, and the
symptom is that 401 rather than anything mentioning a missing table. Apply it
without destroying your data:

```bash
docker compose exec -T db psql -U mqs -d mqsdb < docker/dev-db/init/02-user-creds.sql
```

Then send the header on every call:

```bash
curl -H "X-User-Id: 4510522a-07e1-4dba-98c3-e83bbee3cfe3" \
  http://localhost:8000/api/backtests
```

## Step 5 — run the API

```bash
venv/bin/python -u -m uvicorn server:app --reload --port 8000 --log-level info
```

## Step 6 — point the frontend at it

The frontend ships with fixtures switched on, so it will render happily without
ever calling your API. In `Backtest_Visualiser_FE/.env`:

```dotenv
VITE_USE_FIXTURES=false
VITE_API_BASE_URL=/api
```

Leave `VITE_API_BASE_URL` as `/api`; the Vite dev proxy forwards it to
`DEV_API_PROXY_TARGET` (`http://localhost:8000`), which sidesteps CORS. Restart
`npm run dev` afterwards — Vite reads `.env` once, at startup.

---

## Verify the whole chain

Use the repository's own scripts rather than trusting that it looks fine.
Run these *after* step 4 — before the `app` schema exists, three of the checks
below fail with `relation "app.strategies" does not exist`, which is a missing
setup step rather than a broken database:

```bash
venv/bin/python scripts/smoke_db.py
```

Every check should pass. It is worth reading the output rather than just the
exit code, because it exercises all three connection paths independently —
`psycopg2`, SQLAlchemy async (the API), and the engine's own adapter. A failure
in only the last one means the engine is talking to a different database than
the API, which is exactly the mistake the shared `POSTGRES_*` names prevent.

```bash
venv/bin/python scripts/check_market_data.py
```

Prints first and last bar per ticker. Every ticker should show the range you
seeded, and none should be missing.

---

## Everyday commands

```bash
docker compose up -d db        # start
docker compose stop db         # stop, keep the data
docker compose logs -f db      # tail the server log
docker compose down            # remove the container, keep the data
docker compose down -v         # remove the container AND the data
```

`down -v` is the reset button, and the only way to re-run the init SQL: the
postgres image runs `/docker-entrypoint-initdb.d` scripts **only** when the data
directory is empty. If you edit `01-market-data.sql`, nothing happens until the
volume is destroyed. The full cycle:

```bash
docker compose down -v && docker compose up -d db
# wait for healthy, then
venv/bin/python scripts/seed_dev_db.py
venv/bin/python scripts/seed_strategies.py
```

A psql shell, for when you want to look at the rows yourself:

```bash
docker compose exec db psql -U mqs -d mqsdb
```

---

## Troubleshooting

**`port is already allocated`** — something already holds 5433. Find it with
`lsof -i :5433`, or change the host side of the mapping in `compose.yaml`
(`"5434:5432"`) and update `POSTGRES_PORT` to match.

**`relation "public.market_data" does not exist`** — the init script did not
run, which almost always means the volume already existed when you added it.
`docker compose down -v && docker compose up -d db`.

**`database files are incompatible with server`** — the volume was created by a
different major version of PostgreSQL. `docker compose down -v` and start again;
there is nothing in it worth keeping that the seeder cannot recreate.

**`Send X-User-Id with a public.user_creds id.`** — either you omitted the
header, or `public.user_creds` does not exist because your volume predates
`init/02-user-creds.sql`. See Step 4; the fix does not require destroying the
volume.

**`FMPUnavailable: FMP_API_KEY is missing`** — not an FMP problem. The engine
never consulted the database, because `MARKET_DATA_SOURCE` is unset and
defaults to `fmp`. Set `MARKET_DATA_SOURCE=database` in `.env` (Step 2). The
message names the key rather than the data source, so it reads like a missing
credential when it is really a missing switch.

**Coverage is empty, or a run finishes instantly with no trades** — the table
exists but is empty, or your window is outside the seeded range. Check with
`scripts/check_market_data.py` and seed more days if needed.

**A run succeeds against a database you know is empty** — `data/backfill_cache`
is serving it. Event mode reads those parquet files before it queries Postgres,
so the `NoMarketData` guard never fires; fast mode skips the cache and fails
correctly, which is the tell. `rm -rf data/backfill_cache` and run again. See
Step 3.

**`connection refused` right after `up -d`** — the container is up but
PostgreSQL has not finished starting. Wait for `docker compose ps db` to say
`(healthy)`.

**`error getting credentials ... (-50)` when building** — Docker Desktop's
macOS keychain helper is broken, not this repository. `docker pull <image>`
still works, so `docker compose up -d db` is unaffected; only image *builds*
fail. Fix it by signing out and back in to Docker Desktop, or by removing
`"credsStore": "osxkeychain"` from `~/.docker/config.json`. The database
workflow in this document needs no build at all.

**Backtests read different data than the API** — a stale `POSTGRES_*` value in
your real environment is overriding `.env`. Both `config.py` and the engine
adapter load `.env` with `override=False`, so an exported shell variable wins.
`env | grep POSTGRES` will show it.

**`ImportError: numpy ... symbol not found in flat namespace '_ccopy$NEWLAPACK_'`**
— not a database problem. The venv's numpy binary does not match this machine's
Accelerate framework, and it stops `server.py` importing at all because the
worker chain pulls in pandas. Rebuild numpy and pandas for your interpreter
(`venv/bin/pip install --force-reinstall --no-cache-dir numpy pandas`). Running
the API in its container is the other way out — the image installs Linux wheels
rather than reusing your venv — though see the credentials note above before
relying on it. The database scripts above do not import pandas and work
regardless.

---

## Building the image for deployment

`docker compose up --build` builds and runs the API container locally, wired to
the `db` service — `compose.yaml` points it at `db:5432` for you. It is the
less-travelled path here; if the build fails on credentials, see the note
above. For registry builds, cross-architecture notes, and the
deployment pipeline proper, see [docs/CI_CD.md](docs/CI_CD.md) — that is the
maintained account, and duplicating it here would only let the two drift.
