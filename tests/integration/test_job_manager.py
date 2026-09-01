"""The job pipeline end to end: claim, run, persist, recover.

``run_job`` is invoked synchronously in this process rather than through the
pool. The pool is a scheduling detail — it decides *when* a job runs, not what
it does — and driving it from a test would replace minutes of assertable
behaviour with a wait on a future in another process.

The successful run is real: the vendored engine, the live ``public.market_data``
table, the ``app.*`` tables. It takes minutes, so it happens once in a
module-scoped fixture and every assertion about it reads the rows it left.

Everything here is marked ``db`` and skips cleanly when the database is
unreachable. The strategy row it needs is created and deleted by the test, so
the run does not depend on ``scripts/seed_strategies.py`` having been run, and
nothing it writes survives the module.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert, select, update

from engine.contracts import METRIC_KEYS, EquityPoint
from src.core.config import settings
from src.db.engine import create_sync_engine
from src.db.init import init_database
from src.models import BacktestRun, RunEquityPoint, RunMetrics, RunTrade, Strategy
from src.workers import run_job as run_job_module
from src.workers.reconciler import INTERRUPTED_MESSAGE, reconcile_interrupted_runs
from src.workers.run_job import CANCELLED_MESSAGE, fail_running_run, run_job

pytestmark = pytest.mark.db

_RUNS = BacktestRun.__table__
_METRICS = RunMetrics.__table__
_EQUITY = RunEquityPoint.__table__
_TRADES = RunTrade.__table__
_STRATEGIES = Strategy.__table__

DUMMY_CLASS_PATH = "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy"

# Pinned inside verified coverage — ``market_data`` holds AAPL from 2019-11-11
# to 2026-07-15, so a window computed from today's date returns nothing and
# would make a healthy pipeline look broken. Long enough that the strategy
# opens *and closes* a position: a window with only an entry in it would never
# exercise the round-trip half of the trade persistence.
WINDOW_START = date(2026, 3, 2)
WINDOW_END = date(2026, 7, 15)
INITIAL_CAPITAL = Decimal("100000")

# One ticker and a short indicator lookback. Constructing the strategy warms
# three indicators per ticker straight from the remote database, so this is the
# difference between a test that takes minutes and one that takes many more.
# Every stage of the pipeline still executes.
FAST_PARAMS = {"LOOKBACK_DAYS": 5, "TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1.0}}


@pytest.fixture(scope="module")
def db_engine(require_database: None):
    """A sync engine for the test's own reads and writes.

    ``require_database`` is requested because module-scoped fixtures are
    set up before the function-scoped skip can fire. See tests/conftest.py.
    """

    init_database()
    engine = create_sync_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def strategy_key(db_engine) -> str:
    """A throwaway registry row pointing at the vendored dummy strategy.

    Disabled and archived so it never reaches the catalogue endpoint if a
    teardown is ever interrupted.
    """
    key = f"test-jobmanager-{uuid.uuid4().hex[:8]}"
    with db_engine.begin() as connection:
        connection.execute(
            insert(_STRATEGIES).values(
                key=key,
                name="Job manager integration fixture",
                description="Created and removed by tests/integration/test_job_manager.py",
                tags=["test"],
                universe=["AAPL"],
                param_specs=[],
                kind="builtin",
                class_path=DUMMY_CLASS_PATH,
                status="archived",
                enabled=False,
            )
        )
    try:
        yield key
    finally:
        with db_engine.begin() as connection:
            connection.execute(delete(_RUNS).where(_RUNS.c.strategy_key == key))
            connection.execute(delete(_STRATEGIES).where(_STRATEGIES.c.key == key))


def _insert_run(engine, strategy_key: str, **overrides) -> uuid.UUID:
    """Insert a queued run the way the submission endpoint will, and return its id."""
    run_id = uuid.uuid4()
    values = {
        "id": run_id,
        "name": "job manager integration run",
        "strategy_key": strategy_key,
        "status": "queued",
        "params": dict(FAST_PARAMS),
        "start_date": WINDOW_START,
        "end_date": WINDOW_END,
        "timeframe": "1d",
        "symbol": "AAPL",
        "initial_capital": INITIAL_CAPITAL,
        "progress_pct": 0,
        "engine_version": "test",
        "purpose": "user",
        "cancel_requested": False,
    }
    values.update(overrides)
    with engine.begin() as connection:
        connection.execute(insert(_RUNS).values(**values))
    return run_id


def _run_row(engine, run_id: uuid.UUID):
    with engine.begin() as connection:
        return connection.execute(
            select(_RUNS).where(_RUNS.c.id == run_id)
        ).one_or_none()


def _cleanup_run(engine, run_id: uuid.UUID) -> None:
    with engine.begin() as connection:
        connection.execute(delete(_RUNS).where(_RUNS.c.id == run_id))
    shutil.rmtree(settings.artifact_dir / str(run_id), ignore_errors=True)


@pytest.fixture(scope="module")
def completed_run(db_engine, strategy_key: str):
    """One real backtest, taken from ``queued`` to ``completed`` by ``run_job``."""
    run_id = _insert_run(db_engine, strategy_key)
    outcome = run_job(str(run_id))
    try:
        yield run_id, outcome
    finally:
        _cleanup_run(db_engine, run_id)


# ---------------------------------------------------------------------------
# The successful run
# ---------------------------------------------------------------------------


def test_run_reaches_completed(db_engine, completed_run) -> None:
    run_id, outcome = completed_run
    row = _run_row(db_engine, run_id)

    assert outcome == "completed", row.error_message
    assert row.status == "completed"
    assert row.error_message is None
    assert row.started_at is not None, "the claim must stamp started_at"
    assert row.finished_at is not None
    assert row.finished_at >= row.started_at
    assert row.progress_pct == 100


def test_headline_numbers_are_denormalised_onto_the_run(db_engine, completed_run) -> None:
    """The list endpoint reads only the run row, so these must be filled in."""
    run_id, _ = completed_run
    row = _run_row(db_engine, run_id)

    assert row.final_equity is not None
    assert row.total_return is not None
    assert row.sharpe is not None
    assert row.max_drawdown is not None


def test_metrics_row_matches_the_run_row(db_engine, completed_run) -> None:
    run_id, _ = completed_run
    row = _run_row(db_engine, run_id)
    with db_engine.begin() as connection:
        metrics = connection.execute(
            select(_METRICS).where(_METRICS.c.run_id == run_id)
        ).one()

    for key in METRIC_KEYS:
        assert hasattr(metrics, key), f"run_metrics is missing {key}"

    assert metrics.total_return == row.total_return
    assert metrics.sharpe == row.sharpe
    assert metrics.max_drawdown == row.max_drawdown
    assert metrics.total_trades is not None
    assert metrics.extra["equity_points_stored"] > 0


def test_equity_curve_is_daily_and_ends_at_final_equity(db_engine, completed_run) -> None:
    """The internal-consistency check: the run's final equity is its last point."""
    run_id, _ = completed_run
    row = _run_row(db_engine, run_id)
    with db_engine.begin() as connection:
        points = connection.execute(
            select(_EQUITY).where(_EQUITY.c.run_id == run_id).order_by(_EQUITY.c.seq)
        ).all()

    assert points, "a completed run without an equity curve is not completed"
    assert [point.seq for point in points] == list(range(len(points)))

    days = [point.date for point in points]
    assert days == sorted(days), "the curve must be chronological"
    assert len(set(days)) == len(days), "downsampling must leave one point per day"

    assert row.final_equity == points[-1].equity
    assert all(point.equity > 0 for point in points)


def test_trades_are_round_trips_with_consistent_pnl(db_engine, completed_run) -> None:
    run_id, _ = completed_run
    with db_engine.begin() as connection:
        trades = connection.execute(
            select(_TRADES).where(_TRADES.c.run_id == run_id).order_by(_TRADES.c.seq)
        ).all()
        metrics = connection.execute(
            select(_METRICS).where(_METRICS.c.run_id == run_id)
        ).one()

    assert [trade.seq for trade in trades] == list(range(len(trades)))

    closed = [trade for trade in trades if trade.exit_date is not None]
    # The window is chosen so this holds; a run with no completed round trip
    # would leave the exit_date/exit_price/pnl columns untested.
    assert closed, "the pinned window should produce at least one round trip"
    # total_trades counts round trips only: a lot still open at the end of the
    # window has realised nothing and must not dilute the win rate.
    assert metrics.total_trades == len(closed)

    for trade in trades:
        assert trade.side in {"long", "short"}
        assert trade.quantity > 0
        assert trade.entry_date is not None
        if trade.exit_date is None:
            assert trade.exit_price is None
            assert trade.pnl == 0
        else:
            assert trade.exit_date >= trade.entry_date
            sign = 1 if trade.side == "long" else -1
            expected = (trade.exit_price - trade.entry_price) * trade.quantity * sign
            # Prices and P&L are each rounded to the column's six decimals
            # independently, so the identity holds to within the quantity
            # times half a micro-unit rather than exactly.
            tolerance = float(trade.quantity) * 1e-5 + 1e-6
            assert float(trade.pnl) == pytest.approx(float(expected), abs=tolerance)


def test_artifacts_land_in_the_run_directory(completed_run) -> None:
    run_id, _ = completed_run
    artifact_dir = settings.artifact_dir / str(run_id)
    assert artifact_dir.is_dir()
    assert any(artifact_dir.iterdir()), "the engine writes its CSVs here"


def test_a_finished_run_cannot_be_claimed_again(db_engine, completed_run) -> None:
    """Redelivery safety: the second worker finds the row already claimed."""
    run_id, _ = completed_run
    before = _run_row(db_engine, run_id)

    assert run_job(str(run_id)) == "skipped"

    after = _run_row(db_engine, run_id)
    assert after.status == "completed"
    assert after.finished_at == before.finished_at


# ---------------------------------------------------------------------------
# Failure and cancellation — cheap, because neither reaches the engine loop
# ---------------------------------------------------------------------------


def test_a_run_cancelled_while_queued_never_starts(db_engine, strategy_key) -> None:
    run_id = _insert_run(db_engine, strategy_key, cancel_requested=True)
    try:
        assert run_job(str(run_id)) == "failed"
        row = _run_row(db_engine, run_id)
        assert row.status == "failed"
        assert row.error_message == CANCELLED_MESSAGE
        assert row.finished_at is not None
        assert row.final_equity is None
        with db_engine.begin() as connection:
            points = connection.execute(
                select(_EQUITY).where(_EQUITY.c.run_id == run_id)
            ).all()
        assert points == []
    finally:
        _cleanup_run(db_engine, run_id)


def test_an_unimportable_strategy_fails_the_run_with_a_message(
    db_engine, strategy_key
) -> None:
    """A crash must never read as a success — status and message both move."""
    run_id = _insert_run(db_engine, strategy_key)
    with db_engine.begin() as connection:
        connection.execute(
            update(_STRATEGIES)
            .where(_STRATEGIES.c.key == strategy_key)
            .values(class_path="engine.strategies.nope.strategy:NoSuchStrategy")
        )
    try:
        assert run_job(str(run_id)) == "failed"
        row = _run_row(db_engine, run_id)
        assert row.status == "failed"
        assert row.error_message
        assert len(row.error_message) <= run_job_module.ERROR_MESSAGE_LIMIT
        with db_engine.begin() as connection:
            metrics = connection.execute(
                select(_METRICS).where(_METRICS.c.run_id == run_id)
            ).all()
        assert metrics == [], "a failed run must not leave metrics behind"
    finally:
        with db_engine.begin() as connection:
            connection.execute(
                update(_STRATEGIES)
                .where(_STRATEGIES.c.key == strategy_key)
                .values(class_path=DUMMY_CLASS_PATH)
            )
        _cleanup_run(db_engine, run_id)


def test_an_unknown_run_id_is_not_an_error() -> None:
    """A redelivered job for a deleted run must not crash the worker."""
    assert run_job(str(uuid.uuid4())) == "skipped"
    assert run_job("not-a-uuid") == "skipped"


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


def test_reconciler_fails_runs_whose_process_died(db_engine, strategy_key) -> None:
    run_id = _insert_run(db_engine, strategy_key, status="running", progress_pct=42)
    try:
        assert reconcile_interrupted_runs(db_engine) >= 1
        row = _run_row(db_engine, run_id)
        assert row.status == "failed"
        assert row.error_message == INTERRUPTED_MESSAGE
        assert row.finished_at is not None
    finally:
        _cleanup_run(db_engine, run_id)


def test_reconciler_parks_the_strategy_of_an_interrupted_validation_run(
    db_engine, strategy_key
) -> None:
    """A restart mid-validation must not leave an upload stuck in limbo.

    The strategy leaves ``validating`` only when its validation run reaches a
    verdict. Failing the run without failing the strategy leaves an upload that
    is neither in the catalogue nor runnable, and whose submission message
    still promises it will activate when it passes.
    """
    upload_key = _insert_user_strategy(db_engine)
    run_id = _insert_run(
        db_engine,
        upload_key,
        status="running",
        purpose="validation",
    )
    try:
        reconcile_interrupted_runs(db_engine)

        assert _run_row(db_engine, run_id).status == "failed"
        strategy = _strategy_row(db_engine, upload_key)
        assert strategy.status == "failed_validation"
        assert strategy.enabled is False
        # The run is where the reason lives, so the catalogue has to point at it.
        assert strategy.validation_run_id == run_id
    finally:
        _cleanup_run(db_engine, run_id)
        _delete_strategy(db_engine, upload_key)


def test_reconciler_leaves_an_ordinary_run_s_strategy_alone(
    db_engine, strategy_key
) -> None:
    """Only a validation run has a verdict to write. A normal run has none."""
    upload_key = _insert_user_strategy(db_engine)
    run_id = _insert_run(db_engine, upload_key, status="running", purpose="user")
    try:
        reconcile_interrupted_runs(db_engine)

        assert _run_row(db_engine, run_id).status == "failed"
        assert _strategy_row(db_engine, upload_key).status == "validating"
    finally:
        _cleanup_run(db_engine, run_id)
        _delete_strategy(db_engine, upload_key)


def _insert_user_strategy(engine) -> str:
    """An uploaded strategy mid-validation: the row a restart can strand."""
    key = f"test-upload-{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(
            insert(_STRATEGIES).values(
                key=key,
                name="Interrupted validation fixture",
                description="Created and removed by tests/integration/test_job_manager.py",
                tags=["test"],
                universe=["AAPL"],
                param_specs=[],
                kind="user",
                storage_key=f"strategies/{key}/",
                status="validating",
                enabled=False,
            )
        )
    return key


def _strategy_row(engine, key: str):
    with engine.begin() as connection:
        return connection.execute(
            select(_STRATEGIES).where(_STRATEGIES.c.key == key)
        ).one()


def _delete_strategy(engine, key: str) -> None:
    with engine.begin() as connection:
        connection.execute(delete(_RUNS).where(_RUNS.c.strategy_key == key))
        connection.execute(delete(_STRATEGIES).where(_STRATEGIES.c.key == key))


# ---------------------------------------------------------------------------
# A worker that dies without reporting
# ---------------------------------------------------------------------------


def test_a_dead_worker_s_run_is_failed_with_the_reason(db_engine, strategy_key) -> None:
    """The path a killed process cannot take for itself.

    ``run_job`` marks its own failures, but an OOM kill or a broken pool leaves
    nobody to write the row — so the API process does it from the future's
    callback, and the run stops being a thing the frontend polls forever.
    """
    run_id = _insert_run(db_engine, strategy_key, status="running", progress_pct=17)
    try:
        assert fail_running_run(run_id, "The worker process died: boom", db_engine)

        row = _run_row(db_engine, run_id)
        assert row.status == "failed"
        assert "worker process died" in row.error_message
        assert row.finished_at is not None
    finally:
        _cleanup_run(db_engine, run_id)


def test_failing_a_dead_worker_s_run_never_overwrites_a_terminal_one(
    db_engine, strategy_key
) -> None:
    """A worker that finished and then died must keep the outcome it earned."""
    completed = _insert_run(db_engine, strategy_key, status="completed")
    queued = _insert_run(db_engine, strategy_key)
    try:
        assert fail_running_run(completed, "late news of a death", db_engine) is False
        assert fail_running_run(queued, "late news of a death", db_engine) is False

        assert _run_row(db_engine, completed).status == "completed"
        # Still claimable: the reconciler hands unclaimed runs back to a pool.
        assert _run_row(db_engine, queued).status == "queued"
    finally:
        _cleanup_run(db_engine, completed)
        _cleanup_run(db_engine, queued)


def test_a_dead_worker_parks_the_strategy_it_was_validating(
    db_engine, strategy_key
) -> None:
    """A killed validation worker strands an upload exactly like a restart does."""
    upload_key = _insert_user_strategy(db_engine)
    run_id = _insert_run(
        db_engine, upload_key, status="running", purpose="validation"
    )
    try:
        assert fail_running_run(run_id, "The worker process died: boom", db_engine)
        assert _strategy_row(db_engine, upload_key).status == "failed_validation"
    finally:
        _cleanup_run(db_engine, run_id)
        _delete_strategy(db_engine, upload_key)


def test_reconciler_leaves_queued_and_terminal_runs_alone(db_engine, strategy_key) -> None:
    queued = _insert_run(db_engine, strategy_key)
    done = _insert_run(db_engine, strategy_key, status="completed")
    try:
        reconcile_interrupted_runs(db_engine)
        assert _run_row(db_engine, queued).status == "queued"
        assert _run_row(db_engine, done).status == "completed"
    finally:
        _cleanup_run(db_engine, queued)
        _cleanup_run(db_engine, done)


# ---------------------------------------------------------------------------
# The persistence helpers, in isolation
# ---------------------------------------------------------------------------


def test_equity_downsampling_keeps_the_last_value_of_each_day() -> None:
    """Why last-value: it is what makes final_equity equal the last point."""
    run_id = uuid.uuid4()
    curve = [
        EquityPoint(date(2026, 1, 2), 100.0),
        EquityPoint(date(2026, 1, 2), 105.0),
        EquityPoint(date(2026, 1, 5), 110.0),
        EquityPoint(date(2026, 1, 5), 99.5),
    ]
    rows = run_job_module._equity_rows(run_id, curve)

    assert [row["date"] for row in rows] == [date(2026, 1, 2), date(2026, 1, 5)]
    assert [row["seq"] for row in rows] == [0, 1]
    assert rows[0]["equity"] == Decimal("105.0")
    assert rows[-1]["equity"] == Decimal("99.5")


def test_round_trip_metrics_ignore_lots_still_open() -> None:
    from src.services.trade_pairing import TradeRow

    def trade(seq: int, pnl: float, exit_date: str | None) -> TradeRow:
        return TradeRow(
            seq=seq,
            symbol="AAPL",
            side="long",
            entry_date="2026-01-02",
            exit_date=exit_date,
            entry_price=100.0,
            exit_price=100.0 + pnl if exit_date else None,
            quantity=1.0,
            pnl=pnl,
            return_pct=pnl / 100.0,
            fees=0.0,
        )

    metrics = run_job_module._round_trip_metrics(
        [
            trade(0, 20.0, "2026-01-03"),
            trade(1, -5.0, "2026-01-04"),
            trade(2, 0.0, None),  # still open: not a round trip
        ]
    )

    assert metrics["total_trades"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["profit_factor"] == pytest.approx(4.0)


def test_profit_factor_is_undefined_rather_than_infinite() -> None:
    from src.services.trade_pairing import TradeRow

    winner = TradeRow(
        seq=0,
        symbol="AAPL",
        side="long",
        entry_date="2026-01-02",
        exit_date="2026-01-03",
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        pnl=10.0,
        return_pct=0.1,
        fees=0.0,
    )
    metrics = run_job_module._round_trip_metrics([winner])

    assert metrics["profit_factor"] is None
    assert metrics["win_rate"] == pytest.approx(1.0)


def test_non_finite_numbers_are_stored_as_null() -> None:
    """A NaN sharpe must not become a real-looking zero, or poison the column."""
    assert run_job_module._ratio(float("nan")) is None
    assert run_job_module._ratio(float("inf")) is None
    assert run_job_module._money(None) is None
    assert run_job_module._ratio(0.5) == Decimal("0.5")
