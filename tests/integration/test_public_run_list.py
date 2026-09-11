"""GET /backtests reads public.backtest_runs and only the signed-in user's rows."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from server import app
from src.db.engine import create_sync_engine, dispose_async_engine

pytestmark = pytest.mark.db

ALICE = uuid.UUID("4510522a-07e1-4dba-98c3-e83bbee3cfe3")
BOB = uuid.UUID("bb961a0d-52af-4303-9fdf-1ccc941e3c07")

_TABLE_EXISTS = text(
    """
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'backtest_runs'
    """
)

_HAS_RESULTS = text(
    """
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'backtest_runs'
      AND column_name = 'results'
    """
)

_INSERT_RUN = text(
    """
    INSERT INTO public.backtest_runs (id, owner_id, results)
    VALUES (
      :id,
      :owner_id,
      jsonb_build_object(
        'name', :name,
        'strategy_key', :strategy_key,
        'symbol', :symbol,
        'status', 'completed',
        'start_date', '2023-01-01',
        'end_date', '2024-01-01',
        'total_return', 0.1,
        'sharpe', 1.0,
        'max_drawdown', -0.05
      )
    )
    """
)

_DELETE_RUN = text("DELETE FROM public.backtest_runs WHERE id = :id")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
        test_client.portal.call(dispose_async_engine)


def _require_public_table(connection) -> None:
    if connection.execute(_TABLE_EXISTS).first() is None:
        pytest.skip("public.backtest_runs is not on this database")
    if connection.execute(_HAS_RESULTS).first() is None:
        pytest.skip(
            "public.backtest_runs.results is missing — run "
            "scripts/reshape_public_backtest_runs.sql"
        )


def test_list_requires_a_known_user(client: TestClient) -> None:
    assert client.get("/api/backtests").status_code == 401
    unknown = client.get(
        "/api/backtests",
        headers={"X-User-Id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 401


def test_list_is_scoped_to_the_header_user(client: TestClient) -> None:
    """Alice sees only her row; Bob sees an empty list."""
    alice_run = uuid.uuid4()
    engine = create_sync_engine()
    try:
        with engine.begin() as connection:
            _require_public_table(connection)
            connection.execute(
                _INSERT_RUN,
                {
                    "id": alice_run,
                    "owner_id": ALICE,
                    "name": "scope-test alice",
                    "strategy_key": "mean_reversion",
                    "symbol": "AAPL",
                },
            )
        try:
            alice = client.get("/api/backtests", headers={"X-User-Id": str(ALICE)})
            assert alice.status_code == 200
            alice_ids = {item["id"] for item in alice.json()["items"]}
            assert str(alice_run) in alice_ids
            assert alice.json()["total"] >= 1

            bob = client.get("/api/backtests", headers={"X-User-Id": str(BOB)})
            assert bob.status_code == 200
            bob_ids = {item["id"] for item in bob.json()["items"]}
            assert str(alice_run) not in bob_ids
        finally:
            with engine.begin() as connection:
                connection.execute(_DELETE_RUN, {"id": alice_run})
    finally:
        engine.dispose()
