"""Repository tests against the live MQS PostgreSQL.

Everything here is marked ``db`` and skips cleanly when the database is
unreachable (see ``tests/conftest.py``). These are the only tests that write to
``app.*``; each one removes what it created, so a rerun starts from the same
state and the shared database never accumulates test litter.

They cover what the contract tests structurally cannot: an empty database
cannot prove that a run round-trips, that the strategy aggregates actually
aggregate, or that deleting a run takes its results with it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.db.engine import (
    detached_async_engine,
    dispose_async_engine,
    session_scope,
)
from src.db.init import init_database
from src.models import RunEquityPoint, RunMetrics, RunTrade
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.repositories.runs import RunFilters
from src.services import backtests as backtests_service
from src.services import strategies as strategies_service

pytestmark = pytest.mark.db

SEEDED_KEYS = {"portfolio_1", "portfolio_2", "portfolio_3", "portfolio_dummy"}


def _run(coro):
    """Run one coroutine to completion on an engine belonging to this call.

    The suite has no async plugin, so tests stay synchronous and reach into the
    event loop here. One loop per call, and — via ``detached_async_engine`` —
    one engine per call, disposed on the way out: the pool's connections and
    the loop that owns them share a lifetime, and no other test module's engine
    is ever within reach of that dispose. ``tests/unit/test_api_contract.py``
    holds a module-scoped client with a pool of its own, and it must survive
    however pytest orders the files.
    """

    async def _main():
        try:
            return await coro
        finally:
            await dispose_async_engine()

    with detached_async_engine():
        return asyncio.run(_main())


@pytest.fixture(scope="module", autouse=True)
def schema(database_available: tuple[bool, str]) -> None:
    """Make sure the app schema exists before anything queries it.

    The reachability check is repeated here rather than left to the ``db``
    marker: this fixture is module-scoped, so it runs *before* the marker's
    function-scoped skip and would otherwise fail to connect on an offline
    machine instead of skipping.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)
    init_database()


def test_schema_creation_is_idempotent() -> None:
    # Running it twice is the normal case: every worker process and every
    # script calls it on startup.
    init_database()
    init_database()


def test_seeded_strategies_are_present_and_enabled_correctly() -> None:
    async def scenario():
        async with session_scope() as session:
            everything = await strategies_repo.list_strategies(
                session, include_disabled=True
            )
            enabled = await strategies_repo.list_strategies(session)
        return everything, enabled

    everything, enabled = _run(scenario())

    keys = {row.strategy.key for row in everything}
    assert SEEDED_KEYS <= keys, "run scripts/seed_strategies.py first"

    enabled_keys = {row.strategy.key for row in enabled}
    assert {"portfolio_1", "portfolio_2", "portfolio_3"} <= enabled_keys
    assert "portfolio_dummy" not in enabled_keys

    builtin = next(row for row in everything if row.strategy.key == "portfolio_1")
    assert builtin.strategy.kind == "builtin"
    assert builtin.strategy.class_path
    assert builtin.strategy.universe
    # param_specs must satisfy the frontend's ParameterSpec shape.
    for spec in builtin.strategy.param_specs:
        assert set(spec) >= {"key", "label", "type", "default"}
        assert spec["type"] in {"number", "integer", "percent", "boolean"}


def test_run_round_trips_and_feeds_strategy_aggregates() -> None:
    """Create a completed run with results, then read it back every way."""

    async def scenario():
        async with session_scope() as session:
            run = await runs_repo.create_run(
                session,
                name="repository test run",
                strategy_key="portfolio_dummy",
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 31),
                initial_capital=100_000,
                symbol="MULTI",
                engine_version="test",
                params={"LOOKBACK_DAYS": 30},
            )
            run_id = run.id

            # Stand in for what the worker writes when the engine finishes.
            run.status = "completed"
            run.progress_pct = 100
            run.final_equity = Decimal("110000")
            run.total_return = Decimal("0.1")
            run.sharpe = Decimal("1.25")
            run.max_drawdown = Decimal("-0.05")
            session.add(
                RunMetrics(
                    run_id=run_id,
                    total_return=Decimal("0.1"),
                    cagr=Decimal("1.2"),
                    sharpe=Decimal("1.25"),
                    sortino=Decimal("1.8"),
                    max_drawdown=Decimal("-0.05"),
                    volatility=Decimal("0.2"),
                    win_rate=Decimal("0.5"),
                    profit_factor=Decimal("1.4"),
                    total_trades=2,
                )
            )
            session.add_all(
                [
                    RunEquityPoint(
                        run_id=run_id, seq=0, date=date(2025, 1, 2),
                        equity=Decimal("100000"),
                    ),
                    RunEquityPoint(
                        run_id=run_id, seq=1, date=date(2025, 1, 31),
                        equity=Decimal("110000"),
                    ),
                ]
            )
            session.add(
                RunTrade(
                    run_id=run_id, seq=0, symbol="AAPL", side="long",
                    entry_date=date(2025, 1, 3), exit_date=date(2025, 1, 20),
                    entry_price=Decimal("100"), exit_price=Decimal("110"),
                    quantity=Decimal("100"), pnl=Decimal("1000"),
                    return_pct=Decimal("0.1"), fees=Decimal("1"),
                )
            )

        async with session_scope() as session:
            detail = await runs_repo.get_run(session, run_id)
            listed, total = await runs_repo.list_runs(
                session, RunFilters(strategy_key="portfolio_dummy"), 1, 25
            )
            searched, _ = await runs_repo.list_runs(
                session, RunFilters(search="REPOSITORY TEST"), 1, 25
            )
            filtered_out, _ = await runs_repo.list_runs(
                session, RunFilters(status="queued", strategy_key="portfolio_dummy"),
                1, 25,
            )
            aggregates = await strategies_repo.list_strategies(
                session, include_disabled=True
            )

        summary = await backtests_service.get_backtest(str(run_id))
        return run_id, detail, listed, total, searched, filtered_out, aggregates, summary

    async def cleanup(run_id):
        async with session_scope() as session:
            return await runs_repo.delete_run(session, run_id)

    (
        run_id,
        detail,
        listed,
        total,
        searched,
        filtered_out,
        aggregates,
        summary,
    ) = _run(scenario())

    try:
        assert detail is not None
        assert detail.strategy_name
        assert detail.run.metrics is not None
        assert len(detail.run.equity_points) == 2
        assert len(detail.run.trades) == 1

        assert total >= 1
        assert run_id in {row.run.id for row in listed}
        # Search is case-insensitive across name, symbol, and strategy name.
        assert run_id in {row.run.id for row in searched}
        # A completed run must not answer a queued filter.
        assert run_id not in {row.run.id for row in filtered_out}

        dummy = next(row for row in aggregates if row.strategy.key == "portfolio_dummy")
        assert dummy.run_count >= 1
        assert dummy.best_sharpe is not None
        assert dummy.last_run_at is not None

        # The serialised payload is what the frontend actually parses.
        assert summary is not None
        assert summary.id == str(run_id)
        assert summary.final_equity == 110000.0
        assert summary.metrics.total_trades == 2
        assert summary.equity_curve[-1].equity == 110000.0
        assert summary.trades[0].id.startswith(str(run_id))
    finally:
        assert _run(cleanup(run_id)) is True

    # Deleting the run must take its metrics, curve, and trades with it.
    async def orphans():
        async with session_scope() as session:
            gone = await runs_repo.get_run(session, run_id)
            leftover_metrics = await session.get(RunMetrics, run_id)
        return gone, leftover_metrics

    gone, leftover_metrics = _run(orphans())
    assert gone is None
    assert leftover_metrics is None


def test_unknown_run_id_is_none_not_an_error() -> None:
    # The route turns this into a 404; a non-UUID path segment must not become
    # a 500 on the way there.
    assert runs_repo.parse_run_id("does-not-exist") is None
    assert runs_repo.parse_run_id(str(uuid.uuid4())) is not None
    assert _run(backtests_service.get_backtest("does-not-exist")) is None
    assert _run(backtests_service.get_backtest(str(uuid.uuid4()))) is None


def test_deleting_a_running_run_requests_cancellation_instead() -> None:
    """A run the worker owns cannot be deleted — it is asked to stop."""

    async def scenario():
        async with session_scope() as session:
            run = await runs_repo.create_run(
                session,
                name="cancel me",
                strategy_key="portfolio_dummy",
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 31),
                initial_capital=50_000,
                symbol="MULTI",
                engine_version="test",
            )
            run.status = "running"
            run_id = run.id

        outcome = await backtests_service.delete_backtest(str(run_id))

        async with session_scope() as session:
            after = await runs_repo.get_run(session, run_id)
            cancel_requested = after.run.cancel_requested if after else None
        return run_id, outcome, cancel_requested

    async def cleanup(run_id):
        async with session_scope() as session:
            await runs_repo.delete_run(session, run_id)

    run_id, outcome, cancel_requested = _run(scenario())
    try:
        assert outcome is backtests_service.DeleteOutcome.CANCEL_REQUESTED
        # The row survives: the worker still needs it to report why it stopped.
        assert cancel_requested is True
    finally:
        _run(cleanup(run_id))


def test_deleting_a_queued_run_removes_it() -> None:
    """Nothing has claimed a queued run, so cancelling it would strand the row."""

    async def scenario():
        async with session_scope() as session:
            run = await runs_repo.create_run(
                session,
                name="never claimed",
                strategy_key="portfolio_dummy",
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 31),
                initial_capital=50_000,
                symbol="MULTI",
                engine_version="test",
            )
            run_id = run.id

        outcome = await backtests_service.delete_backtest(str(run_id))

        async with session_scope() as session:
            after = await runs_repo.get_run(session, run_id)
        return run_id, outcome, after

    run_id, outcome, after = _run(scenario())
    assert outcome is backtests_service.DeleteOutcome.DELETED
    # Gone for real: the client dropped it from its cache and must not see it
    # come back on the next list refetch.
    assert after is None


def test_user_strategy_submission_refuses_source_it_could_not_run() -> None:
    """An upload is now scanned before it is stored, and stored before it runs.

    Submitting starts a real validation backtest, so a file that is not a
    strategy is refused outright and *nothing* is written — no store object, no
    registry row, nothing to clean up. The accepted half of this path needs the
    worker pool and lives in ``tests/integration/test_user_strategies.py``.
    """
    from src.schemas.strategies import StrategySubmission
    from src.services.strategy_validation import StrategyValidationError

    submission = StrategySubmission(
        name="Integration upload",
        description="written by the test suite",
        source="# this file defines no strategy\n",
        filename="upload.py",
    )

    async def scenario():
        with pytest.raises(StrategyValidationError) as excinfo:
            await strategies_service.submit_strategy(submission)

        async with session_scope() as session:
            rows = await strategies_repo.list_strategies(
                session, include_disabled=True
            )
        return str(excinfo.value), {row.strategy.key for row in rows}

    message, keys = _run(scenario())

    assert "BasePortfolio" in message
    assert not any(key.startswith("user-integration-upload-") for key in keys)
