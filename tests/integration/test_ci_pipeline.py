"""Real API -> spawned worker -> PostgreSQL -> report/export acceptance proof.

Run independently with ``python -m pytest tests/integration/test_ci_pipeline.py -q``.
The database proof skips BEFORE any connection unless CI_DATABASE_TESTS=1. When
enabled it fails, never skips, on a wrong target or unavailable database. Required:
POSTGRES_HOST=127.0.0.1 (or localhost), POSTGRES_DB=mqs_test, explicit POSTGRES_PORT,
POSTGRES_USER=mqs_test and nonempty POSTGRES_PASSWORD; use POSTGRES_SSLMODE=disable locally.

Only a disposable database may be used. This test creates public.market_data and
seeds synthetic bars when the table is absent; an existing table must match the
fixture exactly. No table is dropped, truncated, or overwritten. App rows remain
for diagnosis until the disposable server is removed. All filesystem outputs go
under pytest's temporary directory, including the worker's materialized source.

The application runs in a fresh interpreter because root conftest.py and other
tests import the frozen settings singleton at collection time. The environment
is installed BEFORE application imports and inherited by the real spawn pool.
The ``ci_db`` marker deliberately avoids the legacy ``db`` marker's offline skip.
The guard tests below require no database and always run.

After upload validation, two identical browser-form submissions include universe,
slippage, commission, empty signal overrides, and a disabled sentiment gate. Both
must produce the same persisted report, including positive cash fees.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = REPO_ROOT / "tests" / "fixtures" / "ci_pipeline_strategy.py"
RUN_TIMEOUT_SECONDS = 90
PROCESS_TIMEOUT_SECONDS = 3 * RUN_TIMEOUT_SECONDS + 60
CAPITAL = 100_000.0
BAR_TIMES = (day_time(9, 30), day_time(12), day_time(16))
SEED_DAYS = 110
WARMUP_DAYS = 30
MARKET_COLUMNS = (
    "ticker, timestamp, date, open_price, high_price, low_price, close_price, volume"
)


def _database_target(environment: Mapping[str, str]) -> dict[str, str] | None:
    """Validate raw environment without importing settings or a database driver."""
    opt_in = environment.get("CI_DATABASE_TESTS", "")
    if opt_in in {"", "0"}:
        return None
    # Explicit raises keep pytest's assertion introspection from rendering the
    # supplied os.environ mapping (which can contain unrelated credentials).
    if opt_in != "1":
        raise AssertionError("CI_DATABASE_TESTS must be exactly 1 to enable the proof")
    if environment.get("POSTGRES_HOST") not in {"localhost", "127.0.0.1"}:
        raise AssertionError(
            "CI_DATABASE_TESTS=1 requires POSTGRES_HOST=localhost or 127.0.0.1; "
            "refusing to connect or seed any other host"
        )
    if environment.get("POSTGRES_DB") != "mqs_test":
        raise AssertionError(
            "CI_DATABASE_TESTS=1 requires POSTGRES_DB=mqs_test; "
            "refusing to connect or seed any other database"
        )
    if environment.get("POSTGRES_USER") != "mqs_test":
        raise AssertionError("CI_DATABASE_TESTS=1 requires POSTGRES_USER=mqs_test")
    for key in ("POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        if not environment.get(key, "").strip():
            raise AssertionError(f"Set {key} explicitly for the test DB")
    port = environment["POSTGRES_PORT"]
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise AssertionError("Invalid POSTGRES_PORT")
    # Canonicalize localhost before application import; no hostname lookup can
    # redirect the API, engine, or fixture outside IPv4 loopback.
    return {
        "CI_DATABASE_TESTS": "1",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_DB": "mqs_test",
        "POSTGRES_PORT": port,
        "POSTGRES_USER": environment["POSTGRES_USER"],
        "POSTGRES_PASSWORD": environment["POSTGRES_PASSWORD"],
        "POSTGRES_SSLMODE": environment.get("POSTGRES_SSLMODE") or "disable",
    }


@pytest.mark.parametrize("opt_in", [None, "", "0"])
def test_no_opt_in_needs_no_database_configuration(opt_in):
    environment = {} if opt_in is None else {"CI_DATABASE_TESTS": opt_in}
    assert _database_target(environment) is None


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("CI_DATABASE_TESTS", "true", "exactly 1"),
        ("POSTGRES_HOST", "production.invalid", "POSTGRES_HOST"),
        ("POSTGRES_HOST", "127.0.0.1,production.invalid", "POSTGRES_HOST"),
        ("POSTGRES_HOST", "", "POSTGRES_HOST"),
        ("POSTGRES_DB", "mqsdb", "POSTGRES_DB"),
        ("POSTGRES_DB", "", "POSTGRES_DB"),
        ("POSTGRES_PORT", "0", "POSTGRES_PORT"),
        ("POSTGRES_USER", "", "POSTGRES_USER"),
        ("POSTGRES_USER", "admin", "POSTGRES_USER"),
        ("POSTGRES_PASSWORD", "", "POSTGRES_PASSWORD"),
    ],
)
def test_opt_in_rejects_unsafe_or_incomplete_target(key, value, message):
    environment = {
        "CI_DATABASE_TESTS": "1",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "mqs_test",
        "POSTGRES_USER": "mqs_test",
        "POSTGRES_PASSWORD": "ci-only-password",
    }
    environment[key] = value
    with pytest.raises(AssertionError, match=message):
        _database_target(environment)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_disposable_target_uses_loopback_without_resolving_hostnames(host):
    target = _database_target(
        {"CI_DATABASE_TESTS": "1", "POSTGRES_HOST": host, "POSTGRES_PORT": "5432",
         "POSTGRES_DB": "mqs_test", "POSTGRES_USER": "mqs_test",
         "POSTGRES_PASSWORD": "ci-only-password"}
    )
    assert target["POSTGRES_HOST"] == "127.0.0.1"
    assert target["POSTGRES_SSLMODE"] == "disable"


@pytest.mark.ci_db
def test_uploaded_strategy_runs_in_spawned_worker_and_exports_persisted_results(tmp_path):
    target = _database_target(os.environ)
    if target is None:
        pytest.skip("Disposable PostgreSQL proof requires explicit CI_DATABASE_TESTS=1")

    # libpq's PGHOSTADDR / PGSERVICE can otherwise override an explicit host
    # and redirect a sync worker even though the async API uses loopback.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(target)
    environment.update(
        {
            "APP_ENV": "test",
            "DEBUG": "false",
            "LOG_LEVEL": "WARNING",
            "API_PREFIX": "/api",
            "MARKET_TIMEZONE": "America/New_York",
            "MARKET_DATA_SOURCE": "database",
            "STRATEGY_STORE_BACKEND": "local",
            "STRATEGY_STORE_S3_BUCKET": "",
            "AWS_EC2_METADATA_DISABLED": "true",
            "MAX_CONCURRENT_RUNS": "1",
            "MAX_BACKTEST_WINDOW_DAYS": "365",
            "VALIDATION_WINDOW_DAYS": "30",
            "VALIDATION_INITIAL_CAPITAL": str(CAPITAL),
            "VALIDATION_TIMEOUT_SECONDS": str(RUN_TIMEOUT_SECONDS),
            "DB_CONNECT_TIMEOUT_SECONDS": "3",
            "PROGRESS_WRITE_INTERVAL_SECONDS": "0.1",
            "RUN_HEARTBEAT_INTERVAL_SECONDS": "0.2",
            "RUN_HEARTBEAT_STALE_SECONDS": "300",
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONHASHSEED": "0",
            "PYTHONOPTIMIZE": "0",
            "PYTHONUNBUFFERED": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "PGHOSTADDR": "127.0.0.1",
            "PGOPTIONS": "-c statement_timeout=15000 -c lock_timeout=5000",
        }
    )
    for key, dirname in (
        ("STRATEGY_STORE_ROOT", "strategies"),
        ("MARKET_CACHE_DIR", "market-cache"),
        ("ARTIFACT_DIR", "artifacts"),
        ("TMPDIR", "worker-temp"),
        ("TEMP", "worker-temp"),
        ("TMP", "worker-temp"),
    ):
        directory = tmp_path / dirname
        directory.mkdir(exist_ok=True)
        environment[key] = str(directory.resolve())

    # A subprocess also isolates application singletons from unrelated pytest
    # tests. No application, engine, store, or job-manager methods are patched.
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--run-ci-proof"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"Disposable API/worker proof exceeded {PROCESS_TIMEOUT_SECONDS}s")
    diagnostic = (result.stdout + "\n" + result.stderr).replace(
        target["POSTGRES_PASSWORD"], "***"
    )
    if result.returncode != 0:
        # Do not let pytest render CompletedProcess: it retains unredacted
        # stdout/stderr alongside the sanitized diagnostic above.
        pytest.fail(diagnostic[-24_000:], pytrace=False)
    assert "CI_PIPELINE_PROOF_OK" in result.stdout, diagnostic[-24_000:]


def _trading_days() -> list[date]:
    days = []
    day = date(2024, 1, 2)
    while len(days) < SEED_DAYS:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _market_rows(tickers: tuple[str, ...]) -> list[tuple]:
    """Synthetic NY weekdays, including a DST transition; no market calendar IO."""
    timezone = ZoneInfo("America/New_York")
    rows = []
    for ticker_index, ticker in enumerate(tickers):
        for day_index, day in enumerate(_trading_days()):
            for bar_index, bar_time in enumerate(BAR_TIMES):
                # Drift plus a sawtooth supplies both up and down returns. All
                # OHLCV values are deterministic and independent of today's date.
                close = round(
                    100 + ticker_index * 80 + day_index * 0.19
                    + ((day_index + ticker_index * 3) % 11 - 5) * 1.1
                    + bar_index * 0.27,
                    2,
                )
                opening = round(close - 0.13, 2)
                rows.append(
                    (
                        ticker, datetime.combine(day, bar_time, timezone), day,
                        opening, round(close + 0.8, 2), round(opening - 0.8, 2),
                        close, 1_000_000 + day_index * 1000 + bar_index * 100,
                    )
                )
    return sorted(rows, key=lambda row: (row[0], row[1]))


def test_synthetic_fixture_covers_every_upload_ticker_and_new_york_dst():
    from src.services.strategy_validation.packaging import DEFAULT_TICKERS

    rows = _market_rows(tuple(DEFAULT_TICKERS))
    assert len(rows) == SEED_DAYS * len(BAR_TIMES) * len(DEFAULT_TICKERS)
    for ticker in DEFAULT_TICKERS:
        ticker_rows = [row for row in rows if row[0] == ticker]
        days = sorted({row[2] for row in ticker_rows})
        assert len(days) == SEED_DAYS and len(days[WARMUP_DAYS:]) >= 80
        assert all(day.weekday() < 5 for day in days)
        assert len({row[1].utcoffset() for row in ticker_rows}) == 2
        assert len({row[6] for row in ticker_rows}) > 80
        for _, timestamp, day, opening, high, low, close, volume in ticker_rows:
            assert timestamp.date() == day and timestamp.time() in BAR_TIMES
            assert 0 < low <= min(opening, close) <= max(opening, close) <= high
            assert volume > 0


def _assert_disposable_connection(connection, cursor):
    # Validate the client's actual target, not inet_server_addr(): a Docker
    # published loopback port reaches a server on its private bridge address.
    # No arbitrary private-network destinations are permitted here.
    target = connection.get_dsn_parameters()
    if not (
        target.get("host") == "127.0.0.1"
        and target.get("hostaddr", "") in {"", "127.0.0.1"}
        and target.get("dbname") == "mqs_test"
        and target.get("user") == "mqs_test"
    ):
        raise AssertionError("Wrong disposable connection target")
    cursor.execute("SELECT current_database(), current_user")
    if cursor.fetchone() != ("mqs_test", "mqs_test"):
        raise AssertionError("Wrong disposable database or role")


@pytest.mark.parametrize("hostaddr", ["", "127.0.0.1"])
def test_disposable_connection_supports_loopback_port_forwarding(hostaddr):
    from types import SimpleNamespace
    from unittest.mock import Mock

    connection = SimpleNamespace(get_dsn_parameters=lambda: {
        "host": "127.0.0.1", "hostaddr": hostaddr,
        "dbname": "mqs_test", "user": "mqs_test",
    })
    cursor = Mock()
    cursor.fetchone.return_value = ("mqs_test", "mqs_test")
    _assert_disposable_connection(connection, cursor)
    cursor.execute.assert_called_once_with("SELECT current_database(), current_user")


@pytest.mark.parametrize("key,value", [
    ("host", "172.18.0.2"), ("hostaddr", "192.0.2.1"),
    ("dbname", "mqsdb"), ("user", "admin"),
])
def test_connected_target_guard_rejects_redirects_before_any_sql(key, value):
    from types import SimpleNamespace
    from unittest.mock import Mock

    target = {"host": "127.0.0.1", "hostaddr": "127.0.0.1",
              "dbname": "mqs_test", "user": "mqs_test", key: value}
    connection = SimpleNamespace(get_dsn_parameters=lambda: target)
    cursor = Mock()
    with pytest.raises(AssertionError, match="Wrong disposable connection target"):
        _assert_disposable_connection(connection, cursor)
    cursor.execute.assert_not_called()


def _seed_disposable_market_data(connection, tickers):
    from psycopg2.extras import execute_values

    rows = _market_rows(tickers)
    assert len(_trading_days()[WARMUP_DAYS:]) >= 80
    with connection, connection.cursor() as cursor:
        # Recheck the actual server identity before the only fixture DDL/DML.
        _assert_disposable_connection(connection, cursor)
        cursor.execute("SELECT to_regclass('public.market_data')")
        if cursor.fetchone()[0] is None:
            cursor.execute(
                """CREATE TABLE public.market_data (
                    ticker TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    date DATE NOT NULL,
                    open_price DOUBLE PRECISION NOT NULL,
                    high_price DOUBLE PRECISION NOT NULL,
                    low_price DOUBLE PRECISION NOT NULL,
                    close_price DOUBLE PRECISION NOT NULL,
                    volume BIGINT NOT NULL,
                    PRIMARY KEY (ticker, timestamp),
                    CHECK (date = (timestamp AT TIME ZONE 'America/New_York')::date),
                    CHECK (low_price > 0 AND high_price >= low_price),
                    CHECK (volume > 0)
                )"""
            )
            cursor.execute(
                "CREATE INDEX ci_market_data_ticker_date ON public.market_data (ticker, date)"
            )
            execute_values(
                cursor, f"INSERT INTO public.market_data ({MARKET_COLUMNS}) VALUES %s", rows
            )
        # An independently rerun test reuses exactly its fixture. Unknown data
        # fails without deletes, upserts, or substituting real market history.
        cursor.execute(f"SELECT {MARKET_COLUMNS} FROM public.market_data ORDER BY ticker, timestamp")
        assert cursor.fetchall() == rows, (
            "public.market_data differs from the deterministic fixture; use a fresh "
            "disposable mqs_test database (existing data was not modified)"
        )


def _get_json(client, path):
    response = client.get(path)
    assert response.status_code == 200, f"GET {path}: {response.status_code} {response.text}"
    return response.json()


def _poll_run(client, run_id):
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    detail = {}
    while time.monotonic() < deadline:
        detail = _get_json(client, f"/api/backtests/{run_id}")
        if detail["status"] in {"failed", "completed"}:
            assert detail["status"] == "completed", detail
            assert detail["progressPct"] == 100, detail
            assert detail["errorMessage"] is None, detail
            return detail
        time.sleep(0.1)
    raise AssertionError(
        f"Run {run_id} exceeded {RUN_TIMEOUT_SECONDS}s: "
        f"status={detail.get('status')} progress={detail.get('progressPct')} "
        f"error={detail.get('errorMessage')}"
    )


def _poll_active_strategy(client, key):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        strategy = _get_json(client, f"/api/strategies/{key}")
        assert strategy["validationState"] != "failed_validation", strategy
        if strategy["validationState"] == "active":
            assert strategy["status"] == "active", strategy
            return strategy
        time.sleep(0.1)
    raise AssertionError(f"Validation completed but strategy {key} never became active")


def _assert_report(detail, tickers, minimum_days):
    curve = detail["equityCurve"]
    assert len(curve) >= minimum_days, "Too few daily equity observations"
    dates = [point["date"] for point in curve]
    assert dates == sorted(set(dates)), "Daily equity must be ordered and unique"
    assert all(detail["startDate"] <= day <= detail["endDate"] for day in dates)
    assert all(date.fromisoformat(day).weekday() < 5 for day in dates)
    for field in ("equity", "benchmark"):
        values = [point[field] for point in curve]
        assert all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
        assert all(value > 0 for value in values)
        assert len(set(values)) > 1, f"{field} must reflect the varying synthetic bars"
    assert curve[0]["benchmark"] == pytest.approx(CAPITAL, abs=0.000001)
    assert detail["finalEquity"] == pytest.approx(curve[-1]["equity"], abs=0.000001)
    expected_return = detail["finalEquity"] / detail["initialCapital"] - 1
    assert detail["totalReturn"] == pytest.approx(expected_return, rel=0, abs=1e-9)
    assert detail["metrics"]["totalReturn"] == pytest.approx(expected_return, rel=0, abs=1e-9)
    assert any(abs(point["equity"] - point["benchmark"]) > 0.01 for point in curve)
    closed = [trade for trade in detail["trades"] if trade["exitDate"] is not None]
    assert closed, "Real context.buy/context.sell calls must persist closed trades"
    assert {trade["symbol"] for trade in closed} == set(tickers)
    assert all(trade["quantity"] > 0 for trade in closed)
    assert all(trade["entryDate"] <= trade["exitDate"] for trade in closed)
    assert any(abs(trade["pnl"]) > 0.01 for trade in closed)
    assert detail["metrics"]["totalTrades"] == len(closed)
    for key, value in detail["metrics"].items():
        if key == "unavailable":
            assert isinstance(value, dict)
            continue
        assert value is None or (isinstance(value, (int, float)) and math.isfinite(value))
    if "reportMetadata" in detail:
        assert isinstance(detail["reportMetadata"], dict)
    if "openPositions" in detail:
        assert isinstance(detail["openPositions"], list)


def _assert_browser_execution_costs(detail, form_params):
    for key in ("universe", "slippageBps", "commissionPerShare", "LOOKBACK_DAYS"):
        assert detail["parameters"][key] == form_params[key], f"Lost browser control {key}"

    trades = detail["trades"]
    assert trades and all(trade["fees"] > 0 for trade in trades), (
        "commissionPerShare from the real form must charge fees on every traded lot"
    )
    # The engine aggregates our intraday seed to its final daily bar. Its first
    # long entry must therefore carry the requested 5 bps over that day's close,
    # independently of report metadata or worker implementation details.
    closes = {
        (row[0], row[2].isoformat()): row[6]
        for row in _market_rows(tuple(form_params["universe"]))
        if row[1].time() == BAR_TIMES[-1]
    }
    slippage = form_params["slippageBps"] / 10_000
    for ticker in form_params["universe"]:
        first_long = next(trade for trade in trades if trade["symbol"] == ticker and trade["side"] == "long")
        unslipped = closes[ticker, first_long["entryDate"]]
        assert first_long["entryPrice"] == pytest.approx(unslipped * (1 + slippage), rel=0, abs=1e-6)

    # Round-trip P&L is gross; open lots have zero realized P&L. Reconcile from
    # the public trade rows and known synthetic final marks, subtracting each
    # lot's fees once, without inventing closing fills or requiring metadata keys.
    reconciled = detail["initialCapital"] + sum(trade["pnl"] - trade["fees"] for trade in trades)
    for trade in trades:
        if trade["exitDate"] is None:
            sign = -1 if trade["side"] == "short" else 1
            mark = closes[trade["symbol"], detail["equityCurve"][-1]["date"]]
            reconciled += sign * (mark - trade["entryPrice"]) * trade["quantity"]
    # Stored prices/quantities have six decimal places; this checks the cash
    # identity to one cent while tolerating that public-report rounding.
    assert detail["finalEquity"] == pytest.approx(reconciled, rel=0, abs=0.01), (
        "Final equity must equal initial capital + realized/unrealized P&L - fees"
    )


def _assert_identical_execution(first, repeated):
    assert first["id"] != repeated["id"], "The repeat must be a separate accepted run"
    for field in ("parameters", "initialCapital", "finalEquity", "totalReturn", "sharpe", "maxDrawdown",
                  "metrics", "equityCurve"):
        assert first[field] == repeated[field], f"Identical browser params changed {field}"
    # Public trade IDs include the run ID; every execution value must match.
    first_trades = [{key: value for key, value in trade.items() if key != "id"} for trade in first["trades"]]
    repeated_trades = [{key: value for key, value in trade.items() if key != "id"} for trade in repeated["trades"]]
    assert first_trades == repeated_trades, "Identical browser params changed trades or fees"
    if "openPositions" in first:
        assert first["openPositions"] == repeated["openPositions"]


def _assert_persisted(connection, detail, purpose):
    from psycopg2.extras import RealDictCursor
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT * FROM app.backtest_reports WHERE id = %s", (detail["id"],))
        row = cursor.fetchone()
        assert row is not None
        assert set(row) == {"id", "owner_id", "created_at", "strategy_key", "name", "version", "results"}
        assert row["strategy_key"] == detail["strategyId"]
        assert row["version"] == 1
        expected = {key: value for key, value in detail.items()
                    if key not in {"status", "progressPct", "errorMessage"}}
        assert row["results"] == expected
        assert row["results"]["reportMetadata"]["purpose"] == purpose
        cursor.execute("SELECT count(*) AS count FROM app.backtest_runs WHERE id = %s", (detail["id"],))
        assert cursor.fetchone()["count"] == 0
        for table in ("run_equity_points", "run_metrics", "run_trades"):
            cursor.execute(f"SELECT count(*) AS count FROM app.{table} WHERE run_id = %s", (detail["id"],))
            assert cursor.fetchone()["count"] == 0


def _assert_csv_rows(response, expected):
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == len(expected), "Export row count differs from detail"
    for exported, original in zip(rows, expected):
        assert set(original) <= set(exported), "Export omitted fields from detail"
        for key, value in original.items():
            if value is None:
                assert exported[key] == ""
            elif isinstance(value, (int, float)):
                assert float(exported[key]) == pytest.approx(value, rel=1e-9, abs=1e-9)
            else:
                assert exported[key] == str(value)


def _assert_exports(client, detail):
    prefix = f"/api/backtests/{detail['id']}/exports"
    for filename, rows in (
        ("equity.csv", detail["equityCurve"]),
        ("trades.csv", detail["trades"]),
        ("metrics.csv", None),
        ("report.json", None),
    ):
        response = client.get(f"{prefix}/{filename}")
        assert response.status_code == 200, f"{filename}: {response.status_code} {response.text}"
        assert filename in response.headers.get("content-disposition", "")
        if filename == "report.json":
            assert response.headers["content-type"].startswith("application/json")
            assert response.json() == detail, "JSON export must preserve the full detail contract"
        else:
            assert response.headers["content-type"].startswith("text/csv")
            if filename == "metrics.csv":
                exported = list(csv.DictReader(io.StringIO(response.text)))
                expected = dict(detail["metrics"], initialCapital=detail["initialCapital"],
                                finalEquity=detail["finalEquity"])
                unavailable = expected.pop("unavailable", {})
                assert len(exported) == len(expected)
                assert {row["metric"] for row in exported} == set(expected)
                for row in exported:
                    assert set(row) == {"metric", "value", "unavailableReason"}
                    value = expected[row["metric"]]
                    if value is None or row["metric"] in unavailable:
                        assert row["value"] == ""
                    else:
                        assert float(row["value"]) == pytest.approx(value, rel=1e-9, abs=1e-9)
                    assert row["unavailableReason"] == unavailable.get(row["metric"], "")
            else:
                _assert_csv_rows(response, rows)
    assert client.get(f"{prefix}/strategy.py").status_code == 404


def _run_ci_proof():
    # This guard also protects a direct invocation of the helper script. It is
    # deliberately before imports that load .env or construct database engines.
    assert _database_target(os.environ) is not None, "Helper requires CI_DATABASE_TESTS=1"

    import psycopg2
    from fastapi.testclient import TestClient

    from src.core.config import settings
    from src.services.strategy_validation.packaging import DEFAULT_TICKERS

    assert settings.postgres_host == "127.0.0.1" and settings.postgres_db == "mqs_test"
    assert settings.strategy_store_backend == "local"
    assert settings.max_concurrent_runs == 1
    for variable, configured in (
        ("STRATEGY_STORE_ROOT", settings.strategy_store_root),
        ("MARKET_CACHE_DIR", settings.market_cache_dir),
        ("ARTIFACT_DIR", settings.artifact_dir),
    ):
        assert configured.resolve() == Path(os.environ[variable]).resolve()

    connection = psycopg2.connect(**settings.psycopg2_connect_kwargs)
    try:
        _seed_disposable_market_data(connection, tuple(DEFAULT_TICKERS))
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS public.user_creds (
                id UUID PRIMARY KEY, email TEXT NOT NULL, display_name TEXT)""")
            cursor.execute("""INSERT INTO public.user_creds (id, email, display_name)
                VALUES ('00000000-0000-0000-0000-000000000001', 'ci@example.invalid', 'CI')
                ON CONFLICT (id) DO NOTHING""")
            cursor.execute("""INSERT INTO public.user_creds (id, email, display_name)
                VALUES ('00000000-0000-0000-0000-000000000002', 'other@example.invalid', 'Other')
                ON CONFLICT (id) DO NOTHING""")
        from server import app
        from src.workers.job_manager import get_job_manager

        source = SOURCE_PATH.read_text(encoding="utf-8")
        with TestClient(app) as client:
            client.headers["X-User-Id"] = "00000000-0000-0000-0000-000000000001"
            manager = get_job_manager()
            assert manager.running and manager.max_workers == 1
            assert manager._pool._mp_context.get_start_method() == "spawn"
            print("CI proof: uploading source and awaiting real worker validation", flush=True)
            response = client.post(
                "/api/strategies",
                json={"name": "Disposable CI strategy", "description": "Synthetic fixture only",
                      "filename": SOURCE_PATH.name, "source": source},
            )
            assert response.status_code == 201, response.text
            uploaded = response.json()
            assert uploaded["status"] == "draft" and uploaded["validationRunId"]
            key = uploaded["id"]
            validation = _poll_run(client, uploaded["validationRunId"])
            _assert_report(validation, DEFAULT_TICKERS, minimum_days=15)
            _assert_persisted(connection, validation, "validation")
            strategy = _poll_active_strategy(client, key)
            assert strategy["validationRunId"] == validation["id"]
            assert strategy["universe"] == list(DEFAULT_TICKERS)
            catalogue = {item["id"]: item for item in _get_json(client, "/api/strategies")["items"]}
            assert catalogue[key]["status"] == "active" and catalogue[key]["runCount"] == 0

            from src.integrations.strategy_store import LocalStrategyStore, strategy_key

            store = LocalStrategyStore(settings.strategy_store_root)
            assert store.get(strategy_key(key), "strategy.py") == source
            config = json.loads(store.get(strategy_key(key), "config.json"))
            assert config["TICKERS"] == list(DEFAULT_TICKERS)
            days = _trading_days()
            form_params = {
                "LOOKBACK_DAYS": 10,
                "universe": ["AAPL", "MSFT"],
                "slippageBps": 5,
                "commissionPerShare": 0.005,
                "signals": [],
                "sentimentGate": {"enabled": False, "threshold": -0.25},
            }
            request = {"name": "Disposable CI browser-form rerun", "strategyKey": key,
                       "startDate": days[WARMUP_DAYS].isoformat(),
                       "endDate": days[-1].isoformat(), "initialCapital": CAPITAL,
                       "mode": "event", "params": form_params}
            reruns = []
            for attempt in range(2):
                print(f"CI proof: submitting identical 80-weekday browser-form run {attempt + 1}/2", flush=True)
                response = client.post("/api/backtests", json=request)
                assert response.status_code == 202, response.text
                accepted = response.json()
                assert accepted["id"] != validation["id"]
                assert accepted["status"] in {"queued", "running", "completed"}, accepted
                detail = _poll_run(client, accepted["id"])
                assert detail["strategyId"] == key
                _assert_report(detail, form_params["universe"], minimum_days=80)
                _assert_browser_execution_costs(detail, form_params)
                _assert_persisted(connection, detail, "user")
                _assert_exports(client, detail)
                assert _get_json(client, f"/api/backtests/{detail['id']}") == detail
                reruns.append(detail)
            _assert_identical_execution(*reruns)
            assert _get_json(client, f"/api/strategies/{key}")["runCount"] == 2
            history = _get_json(client, f"/api/backtests?strategyId={key}")
            assert history["total"] == 2
            assert {item["id"] for item in history["items"]} == {run["id"] for run in reruns}
            other = {"X-User-Id": "00000000-0000-0000-0000-000000000002"}
            assert client.get(f"/api/backtests?strategyId={key}", headers=other).json()["total"] == 0
            prefix = f"/api/backtests/{reruns[0]['id']}"
            for suffix in ("", "/exports/report.json", f"/equity?period=max&endDate={days[-1]}"):
                assert client.get(prefix + suffix, headers=other).status_code == 404
            assert client.delete(prefix, headers=other).status_code == 404

            # Inspect only: the real pool must own a live child that differs
            # from this TestClient host. No artificial job is submitted.
            workers = list(manager._pool._processes.values())
            assert workers and all(worker.pid != os.getpid() and worker.is_alive() for worker in workers)
            assert any(settings.market_cache_dir.rglob("*.parquet")), "Worker did not write its local cache"
            for run in (validation, *reruns):
                assert any((settings.artifact_dir / run["id"]).rglob("*.csv")), "Worker artifacts missing"
            # Drain completed futures before lifespan disposal closes API pools.
            manager.shutdown(wait=True)
        # A fresh API lifespan has no prior in-memory jobs. History and exports
        # must still come entirely from saved reports.
        with TestClient(app) as client:
            client.headers["X-User-Id"] = "00000000-0000-0000-0000-000000000001"
            assert get_job_manager().submitted_run_ids() == []
            assert _get_json(client, f"/api/backtests/{reruns[0]['id']}") == reruns[0]
            _assert_exports(client, reruns[0])
            assert client.delete(f"/api/backtests/{reruns[1]['id']}").status_code == 204
            assert client.get(f"/api/backtests/{reruns[1]['id']}").status_code == 404
            get_job_manager().shutdown(wait=True)
        print("CI_PIPELINE_PROOF_OK", flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    assert sys.argv[1:] == ["--run-ci-proof"], "Use pytest to run this module"
    _run_ci_proof()
