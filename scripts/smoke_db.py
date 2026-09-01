"""Is the database wired up, and how far away is it?

Answers, with numbers, the questions you ask before trusting a deploy: can we
connect, on both drivers the app uses; how long does a round trip take; does
the engine's own market-data query come back; do our tables exist and hold
what we think. Read-only throughout — nothing here writes.

    venv/Scripts/python.exe scripts/smoke_db.py

Exit code is 0 when every check passed, 1 otherwise, so it can gate a deploy.
Latency numbers are medians over a few tries, because the first round trip
pays for connection setup and would misrepresent the steady state.
"""

from __future__ import annotations

import asyncio
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Run as a file, not a module: put the repo root on the path so ``src`` and
# ``engine`` import the same way they do under uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from psycopg2.extras import RealDictCursor
from sqlalchemy import text

from src.core.config import settings
from src.db.engine import create_sync_engine, dispose_async_engine, get_async_engine

# The engine's daily-bar query, verbatim in shape: NY trading hours, grouped to
# a day. If this is slow, backtests are slow, whatever the health checks say.
_DAILY_BARS_SQL = """
    SELECT ticker,
           DATE(timestamp AT TIME ZONE 'America/New_York') AS trade_date,
           MAX(high_price) AS high_price,
           MIN(low_price)  AS low_price,
           SUM(volume)     AS volume
      FROM market_data
     WHERE ticker IN (%s, %s)
       AND timestamp BETWEEN %s AND %s
       AND (timestamp AT TIME ZONE 'America/New_York')::time BETWEEN '09:30' AND '16:00'
     GROUP BY ticker, DATE(timestamp AT TIME ZONE 'America/New_York')
"""

_APP_TABLES = ("strategies", "backtest_runs", "run_metrics", "run_equity_points", "run_trades")

# A single statement is allowed this long before we call it a finding rather
# than wait. Generous, because the box is on a university network.
_STATEMENT_TIMEOUT_MS = 20_000


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    ms: float | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, ms: float | None = None) -> None:
        self.checks.append(Check(name, ok, detail, ms))

    @property
    def failed(self) -> bool:
        return any(not c.ok for c in self.checks)


def _timed(fn, tries: int = 3) -> tuple[object, float]:
    """Run ``fn`` a few times; return its last result and the median ms."""
    samples: list[float] = []
    result = None
    for _ in range(tries):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000)
    return result, statistics.median(samples)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_tcp(report: Report) -> None:
    host, port = settings.postgres_host, int(settings.postgres_port)
    try:
        _, ms = _timed(
            lambda: socket.create_connection((host, port), timeout=5).close(), tries=3
        )
        report.add("tcp reach", True, f"{host}:{port}", ms)
    except OSError as exc:
        report.add("tcp reach", False, f"{host}:{port} — {exc}")


def check_psycopg2(report: Report) -> psycopg2.extensions.connection | None:
    kwargs = dict(settings.psycopg2_connect_kwargs)
    kwargs["cursor_factory"] = RealDictCursor
    try:
        start = time.perf_counter()
        conn = psycopg2.connect(**kwargs)
        ms = (time.perf_counter() - start) * 1000
    except Exception as exc:
        report.add("psycopg2 connect", False, str(exc).strip().splitlines()[0])
        return None

    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
        cur.execute("SELECT version() AS v, current_user AS u, current_database() AS d")
        row = cur.fetchone()
    version = row["v"].split(",")[0]
    report.add("psycopg2 connect", True, f"{version} as {row['u']}@{row['d']}", ms)
    return conn


def check_round_trip(report: Report, conn) -> None:
    def one():
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    _, ms = _timed(one, tries=5)
    # Anything past ~250 ms per trip means a chatty code path (progress polls,
    # per-row reads) will dominate a run. Worth knowing, not a failure.
    report.add("round trip SELECT 1", True, "median of 5", ms)


def check_app_schema(report: Report, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'app' ORDER BY table_name"
        )
        present = {r["table_name"] for r in cur.fetchall()}
    missing = [t for t in _APP_TABLES if t not in present]
    if missing:
        report.add("app schema", False, f"missing tables: {', '.join(missing)}")
        return

    counts = []
    with conn.cursor() as cur:
        for table in _APP_TABLES:
            cur.execute(f'SELECT count(*) AS n FROM app."{table}"')
            counts.append(f"{table}={cur.fetchone()['n']}")
        # The column the heartbeat reconciler depends on. Its absence means an
        # older schema is live and every boot's ADD COLUMN has not run here.
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='app' "
            "AND table_name='backtest_runs' AND column_name='heartbeat_at'"
        )
        has_heartbeat = cur.fetchone() is not None
    report.add("app schema", has_heartbeat, ", ".join(counts) + (
        "" if has_heartbeat else " — backtest_runs.heartbeat_at MISSING"
    ))


def check_market_data(report: Report, conn) -> None:
    """The engine's own query, over the 45 days ending at the newest bar.

    Anchored on the data rather than on today: this checks that the query
    *works*, and a separate line says how stale the data is. Anchoring on today
    conflated the two — an ingestor that stopped last month looked like a broken
    query.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(timestamp)::date AS d FROM market_data WHERE ticker = %s",
            ("AAPL",),
        )
        newest = cur.fetchone()["d"]
    if newest is None:
        report.add("market_data daily bars", False, "no AAPL bars at all")
        return

    age = (date.today() - newest).days
    stale = age > 7
    report.add(
        "market_data freshness", True,
        f"newest AAPL bar {newest} ({age} days old)" + ("  <- STALE: no new bars, is the ingestor running?" if stale else ""),
    )

    end = newest
    start = end - timedelta(days=45)

    def run():
        with conn.cursor() as cur:
            cur.execute(_DAILY_BARS_SQL, ("AAPL", "MSFT", start, end))
            return cur.fetchall()

    try:
        rows, ms = _timed(run, tries=2)
    except psycopg2.errors.QueryCanceled:
        conn.rollback()
        report.add(
            "market_data daily bars", False,
            f"AAPL+MSFT 45d ending {end} timed out after {_STATEMENT_TIMEOUT_MS} ms — "
            "the engine's own query shape; check the (ticker, timestamp) index",
        )
        return
    except Exception as exc:
        report.add("market_data daily bars", False, str(exc).strip().splitlines()[0])
        return

    by_ticker: dict[str, int] = {}
    for r in rows:
        by_ticker[r["ticker"]] = by_ticker.get(r["ticker"], 0) + 1
    if not rows:
        report.add("market_data daily bars", False, f"0 rows for AAPL/MSFT in {start}..{end}", ms)
        return
    detail = "; ".join(f"{t}: {n} trading days" for t, n in sorted(by_ticker.items()))
    report.add("market_data daily bars", True, f"{start}..{end}: {detail}", ms)


def check_index(report: Report, conn) -> None:
    """Does the query above have an index to lean on? Informational."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='market_data'"
        )
        indexes = cur.fetchall()
    covering = [i["indexname"] for i in indexes if "ticker" in i["indexdef"]]
    if covering:
        report.add("market_data index on ticker", True, ", ".join(covering))
    else:
        names = ", ".join(i["indexname"] for i in indexes) or "none"
        report.add(
            "market_data index on ticker", False,
            f"no index mentions ticker (have: {names}) — every backtest scans the table",
        )


def check_sqlalchemy_sync(report: Report) -> None:
    engine = create_sync_engine()
    try:
        def one():
            with engine.connect() as c:
                return c.execute(text("SELECT count(*) FROM app.strategies")).scalar()

        n, ms = _timed(one, tries=3)
        report.add("sqlalchemy sync (worker path)", True, f"app.strategies={n}", ms)
    except Exception as exc:
        report.add("sqlalchemy sync (worker path)", False, str(exc).strip().splitlines()[0])
    finally:
        engine.dispose()


async def _async_probe() -> tuple[int, float]:
    engine = get_async_engine()
    samples = []
    n = 0
    for _ in range(3):
        start = time.perf_counter()
        async with engine.connect() as c:
            n = (await c.execute(text("SELECT count(*) FROM app.backtest_runs"))).scalar()
        samples.append((time.perf_counter() - start) * 1000)
    await dispose_async_engine()
    return int(n), statistics.median(samples)


def check_sqlalchemy_async(report: Report) -> None:
    try:
        n, ms = asyncio.run(_async_probe())
        report.add("sqlalchemy async (API path)", True, f"app.backtest_runs={n}", ms)
    except Exception as exc:
        report.add("sqlalchemy async (API path)", False, str(exc).strip().splitlines()[0])


def check_engine_adapter(report: Report) -> None:
    """The seam the vendored engine actually uses. Same shape, its own code."""
    try:
        from engine.data.db_adapter import EngineDBAdapter
    except Exception as exc:
        report.add("engine db adapter", False, f"import failed: {exc}")
        return

    try:
        adapter = EngineDBAdapter()
        end = date.today()
        start = end - timedelta(days=45)

        def run():
            return adapter.execute_query(
                _DAILY_BARS_SQL, ["AAPL", "MSFT", start, end], fetch=True
            )

        res, ms = _timed(run, tries=2)
        ok = isinstance(res, dict) and res.get("status") == "success"
        rows = len(res.get("data") or []) if ok else 0
        report.add(
            "engine db adapter", ok,
            f"status={res.get('status') if isinstance(res, dict) else type(res).__name__}, "
            f"{rows} rows",
            ms,
        )
    except Exception as exc:
        report.add("engine db adapter", False, str(exc).strip().splitlines()[0])


# ---------------------------------------------------------------------------


def main() -> int:
    # Windows consoles default to cp1252, which cannot print the em dashes in
    # some of the details below. A gate script must not crash on its own
    # report, so degrade unprintable characters instead of raising.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    report = Report()
    print(f"target  {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
          f"  sslmode={settings.postgres_sslmode}  pool={settings.db_pool_size}"
          f"+{settings.db_max_overflow}  connect_timeout={settings.db_connect_timeout_seconds}s")
    print()

    check_tcp(report)
    conn = check_psycopg2(report)
    if conn is not None:
        check_round_trip(report, conn)
        check_app_schema(report, conn)
        check_index(report, conn)
        check_market_data(report, conn)
        conn.close()
    check_sqlalchemy_sync(report)
    check_sqlalchemy_async(report)
    check_engine_adapter(report)

    width = max(len(c.name) for c in report.checks)
    for c in report.checks:
        mark = "OK " if c.ok else "FAIL"
        lat = f"{c.ms:8.1f} ms" if c.ms is not None else " " * 11
        print(f"{mark}  {c.name:<{width}}  {lat}  {c.detail}")

    print()
    print("RESULT: " + ("all checks passed" if not report.failed else "FAILURES above"))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
