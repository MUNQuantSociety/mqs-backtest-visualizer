"""Run ownership from HTTP submission through current and historical list reads."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.dependencies import current_user
from src.api.routes.backtests import router
from src.db.engine import create_sync_engine
from src.repositories import public_runs, runs
from src.schemas.backtests import BacktestRunRequest, BacktestSummary
from src.services import backtests


OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000002")
PAYLOAD = {
    "name": "Owned run", "strategyKey": "sample",
    "startDate": "2026-03-02", "endDate": "2026-03-06",
    "initialCapital": 100_000,
}


@asynccontextmanager
async def _session_scope():
    yield object()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(current_user, "session_scope", _session_scope)
    monkeypatch.setattr(
        current_user.user_creds_repo, "get_user",
        AsyncMock(side_effect=lambda session, owner: object() if owner == OWNER else None),
    )
    monkeypatch.setattr(
        backtests, "_load_runnable_strategy",
        AsyncMock(return_value=backtests._RunnableStrategy("sample", ["SPY"], [])),
    )
    monkeypatch.setattr(backtests, "_validated_coverage", AsyncMock())
    summary = BacktestSummary(
        id=str(uuid.uuid4()), name="Owned run", strategy_id="sample",
        strategy_name="Sample", symbol="SPY", timeframe="1d", status="queued",
        start_date="2026-03-02", end_date="2026-03-06",
        created_at="2026-09-10T12:00:00Z", initial_capital=100_000,
        final_equity=0, total_return=0, sharpe=0, max_drawdown=0,
    )
    create = AsyncMock(return_value=summary)
    monkeypatch.setattr(backtests, "create_backtest_run", create)
    monkeypatch.setattr(backtests, "_dispatch", AsyncMock(return_value=summary))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client, create


def test_post_propagates_validated_owner_to_run_creation(api):
    client, create = api
    response = client.post("/api/backtests", json=PAYLOAD, headers={"X-User-Id": str(OWNER)})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert create.await_args.kwargs["owner_id"] == OWNER


@pytest.mark.parametrize("identity", [None, "", "not-a-uuid", str(OTHER_OWNER)])
@pytest.mark.parametrize("method", ["get", "post"])
def test_missing_malformed_or_unknown_identity_is_rejected(api, identity, method):
    client, create = api
    headers = {} if identity is None else {"X-User-Id": identity}
    response = client.request(method, "/api/backtests", json=PAYLOAD, headers=headers)
    assert response.status_code == 401
    create.assert_not_awaited()


def test_internal_submission_can_still_omit_owner(api):
    _, create = api
    asyncio.run(backtests.submit_backtest_run(BacktestRunRequest(**PAYLOAD)))
    assert create.await_args.kwargs["owner_id"] is None


def test_repository_persists_owner_on_the_queued_run():
    session = SimpleNamespace(add=Mock(), flush=AsyncMock())
    run = asyncio.run(runs.create_run(
        session, name="Owned run", strategy_key="sample",
        start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
        initial_capital=100_000, symbol="SPY", engine_version="test", owner_id=OWNER,
    ))
    assert run.owner_id == OWNER
    assert run.status == "queued"
    session.add.assert_called_once_with(run)


def _fixture_run(number, owner=OWNER, **changes):
    return {
        "id": str(uuid.UUID(int=number)), "owner_id": str(owner),
        "created_at": f"2026-09-{number:02d}T12:00:00Z", "name": f"Run {number}",
        "strategy_key": "sample", "symbol": "SPY", "timeframe": "1d",
        "status": "queued", "purpose": "user", "start_date": "2026-03-02",
        "end_date": "2026-03-06", "initial_capital": 100_000,
        "final_equity": None, "total_return": None, "sharpe": None,
        "max_drawdown": None, **changes,
    }


_APP_COLUMNS = """
    id uuid, owner_id uuid, created_at timestamptz, name text, strategy_key text,
    symbol text, timeframe text, status text, purpose text, start_date date,
    end_date date, initial_capital numeric, final_equity numeric,
    total_return numeric, sharpe numeric, max_drawdown numeric
"""
_PUBLIC_COLUMNS = """
    id uuid, owner_id uuid, created_at timestamptz, name text, strategy_key text,
    symbol text, status text, start_date date, end_date date,
    total_return numeric, sharpe numeric, max_drawdown numeric
"""


class _FixtureReadSession:
    """Execute the production queries on PostgreSQL using only CTE fixture rows.

    Only table references are replaced. No shared rows or schema are written,
    and PostgreSQL itself evaluates the production owner/union/filter clauses.
    """

    def __init__(self, connection, app_rows, public_rows, nested):
        self.connection = connection
        self.app_rows = app_rows
        self.public_rows = public_rows
        self.public_columns = (
            "id uuid, owner_id uuid, created_at timestamptz, results jsonb"
            if nested else _PUBLIC_COLUMNS
        )

    async def execute(self, statement, params):
        sql = str(statement)
        for table, fixture in [
            ("app.backtest_runs", "fixture_app_runs"),
            ("public.backtest_runs", "fixture_public_runs"),
            ("app.strategies", "fixture_strategies"),
        ]:
            sql = sql.replace(table, fixture)
        fixture_ctes = f"""
            WITH fixture_app_runs AS (
                SELECT * FROM jsonb_to_recordset(CAST(:fixture_app AS jsonb))
                    AS source({_APP_COLUMNS})
            ), fixture_public_runs AS (
                SELECT * FROM jsonb_to_recordset(CAST(:fixture_public AS jsonb))
                    AS source({self.public_columns})
            ), fixture_strategies AS (
                SELECT 'sample' AS key, 'Sample strategy' AS name
            ), owned_runs AS (
        """
        sql = sql.replace("WITH owned_runs AS (", fixture_ctes, 1)
        return self.connection.execute(text(sql), {
            **params, "fixture_app": json.dumps(self.app_rows),
            "fixture_public": json.dumps(self.public_rows),
        })


@pytest.mark.db
@pytest.mark.parametrize("nested", [False, True], ids=["public-columns", "public-jsonb"])
def test_current_and_legacy_runs_are_owner_scoped_and_follow_lifecycle(require_database, nested):
    app_rows = [
        _fixture_run(6), _fixture_run(5, OTHER_OWNER),
        _fixture_run(4, purpose="validation"), _fixture_run(3, owner=None),
    ]
    # Null-owned internal runs must also remain absent from the user's list.
    app_rows[-1]["owner_id"] = None
    legacy_rows = [
        _fixture_run(6, status="completed", name="Stale duplicate"),
        _fixture_run(2, status="completed"), _fixture_run(1, OTHER_OWNER),
    ]
    if nested:
        legacy_rows = [
            {**{key: row[key] for key in ("id", "owner_id", "created_at")}, "results": row}
            for row in legacy_rows
        ]

    engine = create_sync_engine()
    try:
        with engine.connect() as connection:
            session = _FixtureReadSession(connection, app_rows, legacy_rows, nested)

            def listing(owner=OWNER, page=1, page_size=25, **filters):
                return asyncio.run(public_runs.list_runs_for_owner(
                    session, owner, public_runs.PublicRunFilters(**filters), page, page_size,
                ))

            rows, total = listing()
            assert total == 2
            assert [row.id.int for row in rows] == [6, 2]
            assert rows[0].results["name"] == "Run 6"
            assert backtests._public_to_summary(rows[0]).status == "queued"
            assert all(row.owner_id == OWNER for row in rows)
            assert listing(page=2, page_size=1)[0][0].id.int == 2
            assert listing(page=2, page_size=1)[1] == 2
            assert listing(search="sample strategy", strategy_key="sample")[1] == 2
            assert listing(search="absent")[1] == 0
            assert listing(status="queued")[1] == 1
            assert [row.id.int for row in listing(OTHER_OWNER)[0]] == [5, 1]

            app_rows[0].update(status="completed", final_equity=110_000, total_return=0.1)
            rows, total = listing(status="completed")
            assert total == 2
            summary = backtests._public_to_summary(rows[0])
            assert summary.final_equity == 110_000
            assert summary.total_return == 0.1

            app_rows[0]["status"] = "failed"
            assert listing(status="failed")[0][0].id.int == 6
            assert listing(status="queued")[1] == 0

            # A newly created run has no duplicate history; deleting it removes
            # it immediately from the next list read without projection cleanup.
            app_rows.append(_fixture_run(7))
            assert listing()[1] == 3
            app_rows.pop()
            assert listing()[1] == 2
    finally:
        engine.dispose()
