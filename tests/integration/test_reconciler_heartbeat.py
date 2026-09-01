"""The reconciler must tell a dead worker from a live one it cannot see.

``status='running'`` only says a worker claimed the row. Whether that worker
still exists is what ``heartbeat_at`` answers. Before this distinction, every
boot failed every running run — which meant a second API instance, a rolling
deploy, or (the way it was found) a second test module opening its own
``TestClient`` killed validation runs that were perfectly healthy.

Rows are inserted directly because the scenario *is* the row state: a worker
that died leaves exactly a ``running`` row with an old beat, and no public
path produces that on purpose.

    pytest tests/integration/test_reconciler_heartbeat.py -v
"""

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, insert, select

from src.core.config import settings
from src.db.engine import create_sync_engine
from src.db.init import init_database
from src.models import BacktestRun
from src.workers.reconciler import INTERRUPTED_MESSAGE, reconcile_interrupted_runs

pytestmark = pytest.mark.db

_RUNS = BacktestRun.__table__

# The disabled harness strategy: rows against it can never be mistaken for a
# student's work, and it is always seeded.
STRATEGY = "portfolio_dummy"


@pytest.fixture(scope="module")
def engine(database_available: tuple[bool, str]) -> Iterator:
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)
    engine = create_sync_engine()
    init_database(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _running_row(heartbeat_at: datetime | None) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(),
        "name": "reconciler heartbeat test",
        "strategy_key": STRATEGY,
        "status": "running",
        "params": {},
        "start_date": date(2025, 1, 2),
        "end_date": date(2025, 1, 31),
        "symbol": "MULTI",
        "initial_capital": 100_000,
        "engine_version": "test",
        "purpose": "user",
        "created_at": now,
        "started_at": now,
        "heartbeat_at": heartbeat_at,
    }


@pytest.fixture
def rows(engine) -> Iterator[dict[str, uuid.UUID]]:
    """Three claimed runs: one alive, one long dead, one from before the column."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=settings.run_heartbeat_stale_seconds * 4)
    alive, dead, legacy = (
        _running_row(now),
        _running_row(stale),
        _running_row(None),
    )
    with engine.begin() as connection:
        connection.execute(insert(_RUNS), [alive, dead, legacy])
    ids = {"alive": alive["id"], "dead": dead["id"], "legacy": legacy["id"]}
    try:
        yield ids
    finally:
        with engine.begin() as connection:
            connection.execute(delete(_RUNS).where(_RUNS.c.id.in_(list(ids.values()))))


def _status(engine, run_id: uuid.UUID) -> tuple[str, str | None]:
    with engine.begin() as connection:
        row = connection.execute(
            select(_RUNS.c.status, _RUNS.c.error_message).where(_RUNS.c.id == run_id)
        ).one()
    return row[0], row[1]


def test_a_run_that_is_still_beating_survives_a_restart(engine, rows) -> None:
    reconcile_interrupted_runs(engine)
    status, error = _status(engine, rows["alive"])
    assert status == "running", "a live worker's run must not be failed by a foreign boot"
    assert error is None


def test_a_run_whose_worker_stopped_beating_is_failed(engine, rows) -> None:
    reconcile_interrupted_runs(engine)
    status, error = _status(engine, rows["dead"])
    assert status == "failed"
    assert error == INTERRUPTED_MESSAGE


def test_a_run_claimed_before_heartbeats_existed_is_failed(engine, rows) -> None:
    """NULL cannot mean alive: nothing will ever write a beat for that row."""
    reconcile_interrupted_runs(engine)
    status, _ = _status(engine, rows["legacy"])
    assert status == "failed"


def test_the_count_reports_only_what_changed(engine, rows) -> None:
    assert reconcile_interrupted_runs(engine) == 2
    # Second pass: the two are terminal now, the live one is still alive.
    assert reconcile_interrupted_runs(engine) == 0
