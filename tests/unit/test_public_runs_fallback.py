"""Dashboard reads survive a database with no warehouse history table.

``public.backtest_runs`` is created by nothing in this repo, so a fresh Docker
dev database does not have it and every list request used to 500 on
``UndefinedTableError``. These cover the existence probe and its backstop, not
the happy path: the union itself is exercised against real Postgres in
``test_run_ownership.py::test_current_and_legacy_runs_are_owner_scoped_and_follow_lifecycle``.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError

from src.repositories import public_runs


OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _UndefinedTable(Exception):
    """Shaped like asyncpg's UndefinedTableError: it carries the SQLSTATE."""

    sqlstate = "42P01"


def _row(number: int) -> dict:
    return {
        "id": uuid.UUID(int=number),
        "owner_id": OWNER,
        "created_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
        "results": {"name": f"run {number}", "status": "completed"},
        "strategy_name": "Sample strategy",
    }


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value

    def mappings(self) -> "_Result":
        return self

    def all(self):
        return self._value


class _Session:
    """Stands in for Postgres: answers the probe, counts, and lists.

    ``history`` is what ``to_regclass`` reports. ``breaks`` makes a statement
    that names the warehouse table fail anyway, which is the only way to reach
    the backstop once the probe has said the table is there.
    """

    def __init__(self, rows: list[dict], *, history: bool, breaks: Exception | None = None) -> None:
        self._rows = rows
        self._history = history
        self._breaks = breaks
        self.statements: list[str] = []
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "to_regclass" in sql:
            return _Result(self._history)
        if "public.backtest_runs" in sql and self._breaks is not None:
            raise self._breaks
        return _Result(len(self._rows) if "count(*)" in sql else self._rows)

    async def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def probes(self) -> int:
        return sum("to_regclass" in sql for sql in self.statements)

    @property
    def history_reads(self) -> int:
        return sum(
            "public.backtest_runs" in sql and "to_regclass" not in sql
            for sql in self.statements
        )


@pytest.fixture(autouse=True)
def _unprobed(monkeypatch):
    # The flag is process-local, so leaving it set would decide the answer for
    # every test that runs afterwards.
    monkeypatch.setattr(public_runs, "_history_available", None)


def _list(session, **filters):
    return asyncio.run(
        public_runs.list_runs_for_owner(
            session, OWNER, public_runs.PublicRunFilters(**filters)
        )
    )


def test_absent_history_table_is_never_queried(caplog):
    session = _Session([_row(2), _row(1)], history=False)

    with caplog.at_level(logging.WARNING):
        rows, total = _list(session)

    assert total == 2
    assert [row.id.int for row in rows] == [2, 1]
    # The point of the probe: no statement naming the missing table is ever
    # sent, so there is no aborted transaction to roll back.
    assert session.history_reads == 0
    assert session.rollbacks == 0
    assert public_runs._HISTORY_RELATION in caplog.text


def test_present_history_table_is_used():
    session = _Session([_row(1)], history=True)

    _list(session)

    assert session.history_reads == 2  # the count and the listing


def test_the_probe_is_asked_once_per_process():
    first = _Session([_row(1)], history=False)
    _list(first)
    second = _Session([_row(1)], history=False)
    _list(second)

    assert first.probes == 1
    assert second.probes == 0
    assert second.history_reads == 0


def test_current_only_reads_keep_filters_and_the_strategy_join():
    session = _Session([_row(1)], history=False)

    _list(session, search="run", status="completed", strategy_key="sample")

    listing = session.statements[-1]
    assert "app.strategies" in listing
    assert ":search" in listing and ":status" in listing and ":strategy_key" in listing


def test_table_dropped_after_the_probe_falls_back_and_logs_the_error(caplog):
    session = _Session(
        [_row(1)], history=True, breaks=DatabaseError("SELECT", {}, _UndefinedTable())
    )

    with caplog.at_level(logging.WARNING):
        rows, total = _list(session)

    assert total == 1 and len(rows) == 1
    # Rolled back before re-reading — without that, Postgres refuses every
    # subsequent statement on the session.
    assert session.rollbacks == 1
    assert "vanished" in caplog.text
    # The traceback is kept: unlike the probe's absence, this one is a surprise.
    assert any(record.exc_info for record in caplog.records)


def test_unrelated_database_errors_still_surface():
    session = _Session(
        [], history=True, breaks=IntegrityError("SELECT", {}, Exception("constraint"))
    )

    # Swallowing this would turn a real fault into a silently short list.
    with pytest.raises(IntegrityError):
        _list(session)
    assert session.rollbacks == 0
