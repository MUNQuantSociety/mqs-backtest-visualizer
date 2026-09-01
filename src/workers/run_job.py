"""Execute one backtest run inside a worker process and persist everything.

This module is the whole of a job's life: claim the row, run the engine, write
what came back, and leave the run in a terminal state no matter how it ended.
It is called by :mod:`src.workers.job_manager` through a
``ProcessPoolExecutor``, which on Windows *spawns* — so everything here has to
work in a fresh interpreter that inherited nothing from the API process.

Three consequences follow from that, and they explain most of the shape below:

* **The database connection is synchronous and belongs to this process.** The
  API's asyncpg pool does not survive a process boundary; a worker that
  inherited one would be reading another process's sockets. A sync engine is
  created here and disposed before returning.
* **The callbacks are built here, not passed in.** ``on_progress`` and
  ``should_cancel`` are closures over a live database connection and cannot be
  pickled, so the ``RunRequest`` is assembled inside the worker from the run id
  alone — the only thing that crosses the boundary.
* **SQL lives in this module rather than in a repository.** ``src/repositories``
  is async, for the API. The worker's statements are the sync counterparts of
  the same tables and are kept together here, which is what the plan's folder
  table means by "workers: sync DB only".

Uploaded strategies travel this same path. The only branch is where the class
comes from — a built-in is imported from ``engine.strategies``, an upload is
copied out of the strategy store into a temporary directory and imported from
there — and the only extra step is at the end, where a run marked
``purpose='validation'`` writes its verdict onto the strategy it validated. A
validation run is otherwise an ordinary run in every respect. Note that
importing an upload *executes* it, in this process, with the credentials this
process holds: see the security note in ``src/services/strategy_validation.py``.

Cancellation is cooperative because there is nothing to signal: killing a pool
worker would leave its run row claimed forever. The engine polls
``should_cancel`` between timestamp groups, this module answers from a
throttled read of ``cancel_requested``, and a cancelled run lands as
``failed`` with the message the frontend expects.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import Engine, delete, func, insert, select, update

from engine.contracts import METRIC_KEYS, EquityPoint, RunRequest, RunResult
from engine.run_single import run_single
from src.core.config import settings
from src.db.engine import create_sync_engine
from src.db.init import init_database
from src.models import BacktestRun, RunEquityPoint, RunMetrics, RunTrade, Strategy
from src.services.trade_pairing import TradeRow, pair_fills

logger = logging.getLogger(__name__)

# Tables rather than ORM classes: this module writes thousands of rows at a
# time and never needs identity-mapped objects.
_RUNS = BacktestRun.__table__
_METRICS = RunMetrics.__table__
_EQUITY = RunEquityPoint.__table__
_TRADES = RunTrade.__table__
_STRATEGIES = Strategy.__table__

# The run table has no ``cancelled`` status and the frontend's Zod enum has no
# member for one, so a cancelled run is a failure whose message says why. This
# exact string is the contract communicated to the frontend session.
CANCELLED_MESSAGE = "Cancelled by user"

# ``error_message`` is read by a human in a browser. A pandas traceback repr
# can run to tens of kilobytes; the first two thousand characters carry the
# exception type and the sentence that matters.
ERROR_MESSAGE_LIMIT = 2000

# Reserved key in ``backtest_runs.params``: the params column is an overlay on
# the strategy's ``config.json`` (whose keys are all upper case), so a
# lower-case ``mode`` is unambiguous and is popped off before the overlay is
# handed to the engine. There is no ``mode`` column to read it from.
MODE_KEY = "mode"
DEFAULT_MODE = "event"

# NUMERIC(20, 6) and NUMERIC(20, 10) hold 14 and 10 integer digits. A value
# past that is not a number worth storing — it is an infinity or a runaway
# ratio — and letting it reach Postgres turns a finished run into a failed one.
_MONEY_LIMIT = Decimal(10) ** 14
_RATIO_LIMIT = Decimal(10) ** 10

# ``create_all`` is idempotent but it is still a round trip per table. Once per
# worker process is enough; the flag is process-local state, which is exactly
# what a spawned worker starts fresh with.
_schema_initialised = False


@dataclass(frozen=True)
class _RunContext:
    """Everything about a claimed run that the engine request needs."""

    run_id: uuid.UUID
    strategy_key: str
    class_path: str
    start_date: date
    end_date: date
    initial_capital: float
    mode: str
    params: dict[str, Any]
    # Where an uploaded strategy was materialised, so it can be deleted when
    # the run is over. ``None`` for a built-in, which is already on disk.
    workdir: Path | None = None


def run_job(run_id: str) -> str:
    """Run one backtest to a terminal state. Returns what happened.

    The return value is ``"completed"``, ``"failed"``, or ``"skipped"`` — the
    last meaning another worker had already claimed the run. It is a value and
    not an exception because this is the top of a worker process: raising past
    here tells the pool nothing useful and loses the reason.

    Safe to call twice with the same id. The claim is a conditional UPDATE, so
    a redelivered job finds the row already ``running`` and returns quietly
    instead of running the backtest a second time.
    """
    parsed = _parse_run_id(run_id)
    if parsed is None:
        logger.error("run_job called with %r, which is not a run id", run_id)
        return "skipped"

    outcome = "skipped"
    engine = create_sync_engine()
    try:
        outcome = _execute(engine, parsed)
        return outcome
    finally:
        if outcome != "skipped":
            # A validation run is an ordinary run whose result also decides
            # whether an uploaded strategy becomes selectable. In the ``finally``
            # so it happens on every terminal path, including the ones that
            # never reached the engine.
            apply_validation_outcome(engine, parsed, outcome)
        engine.dispose()


def _execute(engine: Engine, parsed: uuid.UUID) -> str:
    """Claim the run, execute it, and leave it terminal. See :func:`run_job`."""
    context: _RunContext | None = None
    heartbeat: _RunHeartbeat | None = None
    try:
        _ensure_schema(engine)

        if not _claim(engine, parsed):
            logger.info("Run %s was already claimed; nothing to do.", parsed)
            return "skipped"

        try:
            context = _load_context(engine, parsed)
        except Exception as exc:
            # A missing strategy, an unrunnable one, a row that vanished: the
            # run is claimed, so it must not be left in ``running``.
            return _fail(engine, parsed, _describe(exc))

        heartbeat = _RunHeartbeat(engine, parsed)
        heartbeat.start()
        if heartbeat.should_cancel():
            # Cancelled while it sat in the queue. Answering now saves the
            # minutes of data loading that precede the first cancellation poll.
            return _fail(engine, parsed, CANCELLED_MESSAGE)

        result = run_single(_build_request(context, heartbeat))

        if result.status == "completed":
            try:
                _persist_success(engine, context, result)
            except Exception as exc:
                # The backtest was fine; storing it was not. Saying "completed"
                # with no metrics, curve, or trades would be a lie the client
                # cannot detect, so this is a failure with an honest message.
                logger.exception("Run %s produced results that would not store", parsed)
                return _fail(engine, parsed, f"result could not be stored: {_describe(exc)}")
            logger.info("Run %s completed", parsed)
            return "completed"

        if result.status == "cancelled":
            return _fail(engine, parsed, CANCELLED_MESSAGE)

        return _fail(engine, parsed, result.error or "the run failed without a reason")

    except Exception as exc:  # pragma: no cover - needs a broken database
        logger.exception("Run %s crashed outside the engine", parsed)
        _fail(engine, parsed, _describe(exc))
        return "failed"
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if context is not None:
            _remove_workdir(context.workdir)


# ---------------------------------------------------------------------------
# Claiming, progress, cancellation
# ---------------------------------------------------------------------------


def _ensure_schema(engine: Engine) -> None:
    """Create ``app.*`` once per worker process, before anything reads it."""
    global _schema_initialised
    if _schema_initialised:
        return
    init_database(engine)
    _schema_initialised = True


def _claim(engine: Engine, run_id: uuid.UUID) -> bool:
    """Take ownership of a queued run. False if someone else got there first.

    The ``status = 'queued'`` predicate is the entire concurrency design: two
    workers handed the same id both issue this UPDATE, Postgres serialises
    them, and exactly one sees a row count of 1. It is also what makes a
    delete-while-queued safe — the repository's delete carries the mirror-image
    predicate, so whichever statement lands second matches nothing.
    """
    with engine.begin() as connection:
        result = connection.execute(
            update(_RUNS)
            .where(_RUNS.c.id == run_id, _RUNS.c.status == "queued")
            .values(
                status="running",
                started_at=func.now(),
                heartbeat_at=func.now(),
                progress_pct=0,
                # A resubmitted run should not display the previous attempt's
                # error while it is running.
                error_message=None,
            )
        )
    return bool(result.rowcount)


class _RunHeartbeat:
    """The worker's two-way link to the run row while the engine works.

    One object serves both engine callbacks because they want the same round
    trip: writing progress and reading the cancellation flag are the same row.
    The engine calls ``on_progress`` on every one-percent change and
    ``should_cancel`` once per timestamp group — thousands of calls a minute on
    a fast window — so both are throttled to
    ``settings.progress_write_interval_seconds``. Without that floor a run
    spends its time talking to a remote database instead of simulating, for a
    progress bar nobody can read at that rate anyway.

    Database errors here are logged and swallowed. A progress bar that stops
    moving is a cosmetic problem; a backtest that dies twenty minutes in
    because a status write timed out is not.
    """

    def __init__(self, engine: Engine, run_id: uuid.UUID) -> None:
        self._engine = engine
        self._run_id = run_id
        self._interval = max(float(settings.progress_write_interval_seconds), 0.0)
        self._pending_pct = 0
        self._written_pct = -1
        self._cancelled = False
        # Negative infinity rather than "now": the first poll must not be
        # throttled, because a run cancelled while queued should stop before it
        # loads a single bar.
        self._last_poll = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- liveness ---------------------------------------------------------
    #
    # Progress is not liveness. The engine makes no callback at all while it
    # loads bars — minutes, on a cold cache against the remote database — so a
    # reconciler that judged "alive" by progress writes would kill healthy
    # runs during exactly the phase that takes longest. This thread beats on
    # its own clock, and stops the moment the run leaves the engine.

    def start(self) -> None:
        """Begin beating on a daemon thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._beat_forever,
            name=f"heartbeat-{self._run_id}",
            daemon=True,  # never keeps a worker process alive on its own
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop beating and wait briefly for the thread to notice."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _beat_forever(self) -> None:
        interval = max(float(settings.run_heartbeat_interval_seconds), 0.5)
        while not self._stop.wait(interval):
            try:
                with self._engine.begin() as connection:
                    connection.execute(
                        update(_RUNS)
                        .where(_RUNS.c.id == self._run_id)
                        .values(heartbeat_at=func.now())
                    )
            except Exception as exc:
                # Same policy as progress: a missed beat is a cosmetic problem
                # with a large grace window; a backtest killed by a transient
                # database error is not.
                logger.warning(
                    "Run %s: heartbeat failed (%s); the run continues",
                    self._run_id,
                    _describe(exc),
                )

    def on_progress(self, pct: int, stage: str) -> None:
        """Engine callback: record progress, write it at most once a second."""
        self._pending_pct = max(0, min(100, int(pct)))
        logger.debug("Run %s: %d%% (%s)", self._run_id, self._pending_pct, stage)
        self._poll()

    def should_cancel(self) -> bool:
        """Engine callback: has anyone asked this run to stop?

        Answers from the last read between polls. The cost of learning about a
        cancellation up to a second late is a second of simulation; the cost of
        asking every timestamp group is the run.
        """
        self._poll()
        return self._cancelled

    def _poll(self) -> None:
        if self._cancelled:
            # Nothing left to learn, and the engine is about to unwind — no
            # point writing progress for a run that is stopping.
            return

        now = time.monotonic()
        if now - self._last_poll < self._interval:
            return
        self._last_poll = now

        try:
            with self._engine.begin() as connection:
                if self._pending_pct != self._written_pct:
                    # One statement for both jobs: the UPDATE that publishes
                    # progress returns the flag we were going to SELECT anyway.
                    row = connection.execute(
                        update(_RUNS)
                        .where(_RUNS.c.id == self._run_id)
                        .values(progress_pct=self._pending_pct)
                        .returning(_RUNS.c.cancel_requested)
                    ).first()
                    self._written_pct = self._pending_pct
                else:
                    row = connection.execute(
                        select(_RUNS.c.cancel_requested).where(
                            _RUNS.c.id == self._run_id
                        )
                    ).first()
        except Exception as exc:
            logger.warning(
                "Run %s: progress/cancel poll failed (%s); the run continues",
                self._run_id,
                _describe(exc),
            )
            return

        if row is not None and bool(row[0]):
            self._cancelled = True
            logger.info("Run %s: cancellation requested", self._run_id)


# ---------------------------------------------------------------------------
# Building the engine request
# ---------------------------------------------------------------------------


def _load_context(engine: Engine, run_id: uuid.UUID) -> _RunContext:
    """Read the claimed run and the strategy it names, or explain why not."""
    with engine.begin() as connection:
        row = connection.execute(
            select(
                _RUNS.c.strategy_key,
                _RUNS.c.start_date,
                _RUNS.c.end_date,
                _RUNS.c.initial_capital,
                _RUNS.c.params,
                _STRATEGIES.c.kind,
                _STRATEGIES.c.class_path,
                _STRATEGIES.c.storage_key,
            )
            .select_from(
                _RUNS.join(_STRATEGIES, _STRATEGIES.c.key == _RUNS.c.strategy_key)
            )
            .where(_RUNS.c.id == run_id)
        ).first()

    if row is None:
        raise LookupError(f"run {run_id} disappeared between claim and load")

    params = dict(row.params or {})
    mode = str(params.pop(MODE_KEY, DEFAULT_MODE) or DEFAULT_MODE).lower().strip()

    class_path, workdir = _resolve_class_path(
        run_id, row.strategy_key, row.kind, row.class_path, row.storage_key
    )

    return _RunContext(
        run_id=run_id,
        strategy_key=row.strategy_key,
        class_path=class_path,
        start_date=row.start_date,
        end_date=row.end_date,
        initial_capital=float(row.initial_capital),
        mode=mode or DEFAULT_MODE,
        params=params,
        workdir=workdir,
    )


def _resolve_class_path(
    run_id: uuid.UUID,
    strategy_key: str,
    kind: str,
    class_path: str | None,
    storage_key: str | None,
) -> tuple[str, Path | None]:
    """Where to import this strategy's class from, and what to clean up after.

    Built-ins carry a dotted path to a vendored class and need nothing else.
    An upload is a pair of objects in the strategy store, so it is copied into
    a temporary directory and imported from there; the loader registers the
    module under a per-run name, which is why what comes back is still an
    ordinary ``"module:ClassName"`` path that ``run_single`` resolves without
    knowing an upload was involved. This is the *only* place the pipeline
    branches on ``kind``.
    """
    if kind == "user":
        return _materialize_user_strategy(run_id, strategy_key, storage_key)
    if class_path:
        return class_path, None
    raise RuntimeError(f"strategy {strategy_key!r} has no class_path to run")


def _materialize_user_strategy(
    run_id: uuid.UUID, strategy_key: str, storage_key: str | None
) -> tuple[str, Path]:
    """Bring an uploaded strategy to disk and import the class it defines.

    SECURITY: importing it *executes* it. From this line on, user-supplied
    Python is running inside a worker process that holds admin credentials to
    the production database. The upload-time source scan
    (``src/services/strategy_validation.py``) is a speed bump against
    accidents, not a sandbox, and this temporary directory is not a jail —
    real isolation (a container, no network egress, a database role scoped to
    ``public.market_data``) is deferred work that has to land before this
    pipeline is exposed beyond the club.
    """
    if not storage_key:
        raise RuntimeError(
            f"strategy {strategy_key!r} is an upload with no storage key; "
            "its source was never stored and it cannot be run"
        )

    # Imported here rather than at module scope: importing the loader pulls in
    # the engine (and pandas), which a worker only needs once it has a run.
    from engine.strategies.user_loader import load_user_strategy
    from src.integrations.strategy_store import get_strategy_store

    workdir = Path(tempfile.mkdtemp(prefix=f"mqs-user-{run_id.hex[:8]}-"))
    try:
        loaded = load_user_strategy(
            storage_key=storage_key,
            store=get_strategy_store(),
            dest_dir=workdir,
            token=run_id.hex,
        )
    except BaseException:
        _remove_workdir(workdir)
        raise
    return loaded.class_path, workdir


def _remove_workdir(workdir: Path | None) -> None:
    """Delete a materialised upload. Best effort — it is a copy, not the record.

    The store holds the real source; this directory only exists because Python
    imports files rather than strings. A copy that will not delete (an antivirus
    scan, a OneDrive lock) must not turn a finished run into a failed one.
    """
    if workdir is None:
        return
    shutil.rmtree(workdir, ignore_errors=True)


def _build_request(context: _RunContext, heartbeat: _RunHeartbeat) -> RunRequest:
    """Assemble the engine request — inside the worker, where it has to be."""
    artifact_dir = Path(settings.artifact_dir) / str(context.run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    return RunRequest(
        run_id=str(context.run_id),
        strategy_key=context.strategy_key,
        class_path=context.class_path,
        start_date=context.start_date,
        end_date=context.end_date,
        initial_capital=context.initial_capital,
        mode=context.mode,
        params=dict(context.params),
        artifact_dir=str(artifact_dir),
        on_progress=heartbeat.on_progress,
        should_cancel=heartbeat.should_cancel,
    )


# ---------------------------------------------------------------------------
# Persisting a finished run
# ---------------------------------------------------------------------------


def _persist_success(engine: Engine, context: _RunContext, result: RunResult) -> None:
    """Write metrics, curve, trades, and the run row in one transaction.

    All of it or none of it: a run whose row says ``completed`` while its
    equity curve is half-written renders as a chart with a cliff in it, and
    nothing downstream would ever notice.
    """
    trades = pair_fills(result.fills)
    trade_rows = [_trade_row(context.run_id, trade) for trade in trades]
    equity_rows = _equity_rows(context.run_id, result.equity_curve)
    metrics_row = _metrics_row(context, result, trades, len(equity_rows))

    final_equity = result.final_equity
    if final_equity is None and result.equity_curve:
        final_equity = result.equity_curve[-1].equity

    with engine.begin() as connection:
        # Results are keyed by run id with no version, so a re-run of the same
        # id has to replace rather than collide on the primary key.
        connection.execute(delete(_METRICS).where(_METRICS.c.run_id == context.run_id))
        connection.execute(delete(_EQUITY).where(_EQUITY.c.run_id == context.run_id))
        connection.execute(delete(_TRADES).where(_TRADES.c.run_id == context.run_id))

        connection.execute(insert(_METRICS), [metrics_row])
        if equity_rows:
            connection.execute(insert(_EQUITY), equity_rows)
        if trade_rows:
            connection.execute(insert(_TRADES), trade_rows)

        connection.execute(
            update(_RUNS)
            .where(_RUNS.c.id == context.run_id)
            .values(
                status="completed",
                # Denormalised onto the run row because the list endpoint sorts
                # and renders these four and must not join to do it.
                final_equity=_money(final_equity),
                total_return=_ratio(result.metrics.get("total_return")),
                sharpe=_ratio(result.metrics.get("sharpe")),
                max_drawdown=_ratio(result.metrics.get("max_drawdown")),
                progress_pct=100,
                error_message=None,
                finished_at=func.now(),
            )
        )


def _equity_rows(
    run_id: uuid.UUID, curve: Sequence[EquityPoint]
) -> list[dict[str, Any]]:
    """Downsample the curve to one row per day, keeping the day's last value.

    Event mode samples at the strategy's poll interval — sixty seconds for some
    portfolios — which is roughly a hundred thousand points for a year-long
    run. The frontend charts trading days, so the other ninety-nine percent of
    those rows are storage and transfer for pixels nobody sees.

    Last value rather than close-of-day average because the run's final equity
    is the last sample of the last day: keeping the last value is what makes
    ``final_equity == equity_curve[-1].equity`` hold after downsampling.
    """
    by_day: dict[date, EquityPoint] = {}
    for point in curve:
        by_day[point.date] = point

    rows: list[dict[str, Any]] = []
    for seq, day in enumerate(sorted(by_day)):
        point = by_day[day]
        rows.append(
            {
                "run_id": run_id,
                "seq": seq,
                "date": day,
                "equity": _money_required(point.equity, "equity"),
                "benchmark": _money(point.benchmark),
            }
        )
    return rows


def _trade_row(run_id: uuid.UUID, trade: TradeRow) -> dict[str, Any]:
    """One ``run_trades`` row. The pairing service reports ISO date strings."""
    return {
        "run_id": run_id,
        "seq": trade.seq,
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_date": date.fromisoformat(trade.entry_date),
        "exit_date": date.fromisoformat(trade.exit_date) if trade.exit_date else None,
        "entry_price": _money_required(trade.entry_price, "entry_price"),
        "exit_price": _money(trade.exit_price),
        "quantity": _money_required(trade.quantity, "quantity"),
        "pnl": _money_required(trade.pnl, "pnl"),
        "return_pct": _ratio_required(trade.return_pct, "return_pct"),
        "fees": _money_required(trade.fees, "fees"),
    }


def _metrics_row(
    context: _RunContext,
    result: RunResult,
    trades: Sequence[TradeRow],
    stored_equity_points: int,
) -> dict[str, Any]:
    """The ``run_metrics`` row: engine numbers plus the round-trip ones."""
    engine_metrics = dict(result.metrics or {})
    round_trip = _round_trip_metrics(trades)

    row: dict[str, Any] = {"run_id": context.run_id}
    for key in METRIC_KEYS:
        # The engine leaves the three trade-shaped metrics as None: it only
        # ever sees one-leg fills, so pairing them is this side's job.
        value = round_trip[key] if key in round_trip else engine_metrics.get(key)
        row[key] = int(value or 0) if key == "total_trades" else _ratio(value)

    row["extra"] = {
        "fill_count": len(result.fills),
        "closed_trades": row["total_trades"],
        "open_lots": len(trades) - row["total_trades"],
        "equity_samples": len(result.equity_curve),
        "equity_points_stored": stored_equity_points,
        "mode": context.mode,
    }
    return row


def _round_trip_metrics(trades: Sequence[TradeRow]) -> dict[str, float | int | None]:
    """Win rate, profit factor, and trade count from *closed* round trips.

    Open lots are excluded deliberately. The pairing service emits them with
    ``pnl = 0`` because nothing has been realised, and counting them would
    inflate the trade count and drag the win rate toward a break-even the
    strategy never took.
    """
    closed = [trade for trade in trades if trade.exit_date is not None]
    if not closed:
        return {"win_rate": None, "profit_factor": None, "total_trades": 0}

    wins = [trade for trade in closed if trade.pnl > 0]
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = -sum(trade.pnl for trade in closed if trade.pnl < 0)

    return {
        "win_rate": len(wins) / len(closed),
        # Undefined rather than infinite when nothing lost money: NUMERIC has
        # no honest representation of "divided by zero", and a null reads as
        # "not applicable" where a huge number would read as a real result.
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "total_trades": len(closed),
    }


# ---------------------------------------------------------------------------
# Validation runs
# ---------------------------------------------------------------------------

# What a finished validation run does to the strategy it validated. A pass
# makes the upload selectable; a failure parks it, with the run row still there
# for the student to open and read the error out of.
_VALIDATION_OUTCOMES = {
    "completed": ("active", True),
    "failed": ("failed_validation", False),
}


def apply_validation_outcome(engine: Engine, run_id: uuid.UUID, outcome: str) -> None:
    """Write a validation run's verdict onto the strategy it proved.

    One statement, guarded by the run's own ``purpose``, so an ordinary run
    costs a single UPDATE that matches nothing rather than an extra read. It is
    also why nothing here has to remember whether this run was a validation:
    the row says so, and the strategy is only touched when it does.

    Public because the worker is not the only thing that can end a validation
    run: the startup reconciler fails the runs whose processes died with the
    last server, and a strategy whose verdict was never written stays
    ``validating`` forever — invisible in the catalogue and impossible to run.
    Every caller must reach the same conclusion, so they call this.

    ``validation_run_id`` is set for failures too — that is the run whose
    ``error_message`` explains what went wrong, and a student cannot find it
    from the catalogue any other way.
    """
    status, enabled = _VALIDATION_OUTCOMES.get(outcome, (None, None))
    if status is None:
        return

    validated_key = (
        select(_RUNS.c.strategy_key)
        .where(_RUNS.c.id == run_id, _RUNS.c.purpose == "validation")
        .scalar_subquery()
    )

    try:
        with engine.begin() as connection:
            result = connection.execute(
                update(_STRATEGIES)
                .where(
                    _STRATEGIES.c.key == validated_key,
                    # Built-ins are never validated by a run and must not be
                    # disabled by one, whatever a hand-edited row says.
                    _STRATEGIES.c.kind == "user",
                )
                .values(
                    status=status, enabled=enabled, validation_run_id=run_id
                )
            )
    except Exception:
        # The run itself is already recorded correctly. Losing the strategy
        # flip leaves an upload stuck in ``validating``, which is visible and
        # fixable; raising here would lose the run's own outcome as well.
        logger.exception("Validation outcome for run %s could not be applied", run_id)
        return

    if result.rowcount:
        logger.info(
            "Validation run %s %s; its strategy is now %s", run_id, outcome, status
        )


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def _fail(engine: Engine, run_id: uuid.UUID | None, message: str) -> str:
    """Move a run to ``failed`` with a message a student can read.

    Best effort by design: this is the path taken when things have already gone
    wrong, and the database is a plausible thing to have gone wrong. If even
    this write fails, the reconciler at the next startup finds the row still
    ``running`` and finishes the job.
    """
    if run_id is None:  # pragma: no cover - guarded by the caller
        return "failed"

    truncated = (message or "the run failed without a reason")[:ERROR_MESSAGE_LIMIT]
    try:
        with engine.begin() as connection:
            connection.execute(
                update(_RUNS)
                .where(_RUNS.c.id == run_id)
                .values(
                    status="failed",
                    error_message=truncated,
                    finished_at=func.now(),
                )
            )
    except Exception:
        logger.exception("Run %s could not be marked failed", run_id)
    logger.warning("Run %s failed: %s", run_id, truncated)
    return "failed"


def fail_running_run(
    run_id: uuid.UUID | str, message: str, engine: Engine | None = None
) -> bool:
    """Mark a run ``failed`` from outside the process that was running it.

    The worker reports its own failures, but it cannot report the one failure
    that kills it: a process taken away by the OOM killer, a ``SIGKILL``, a
    pool broken under it. Nobody is left to write the row, so the API process
    that submitted the job does it from the future's callback — otherwise the
    run says ``running`` with no progress and no error until the next restart,
    and the frontend polls it for as long as the student is willing to wait.

    The ``status = 'running'`` predicate is what makes this safe to call on any
    dead future: a run that already reached a terminal state keeps the outcome
    it earned, and a run that was never claimed stays ``queued`` for the
    reconciler to requeue.

    Returns whether this call is the one that ended the run.
    """
    parsed = _parse_run_id(run_id)
    if parsed is None:
        logger.error("fail_running_run called with %r, which is not a run id", run_id)
        return False

    owned = engine is None
    engine = engine or create_sync_engine()
    truncated = (message or "the run failed without a reason")[:ERROR_MESSAGE_LIMIT]
    try:
        with engine.begin() as connection:
            result = connection.execute(
                update(_RUNS)
                .where(_RUNS.c.id == parsed, _RUNS.c.status == "running")
                .values(
                    status="failed",
                    error_message=truncated,
                    finished_at=func.now(),
                )
            )
        if not result.rowcount:
            return False

        logger.warning("Run %s failed: %s", parsed, truncated)
        # A dead worker takes its validation verdict with it, and an upload
        # whose verdict is never written is stuck in ``validating``.
        apply_validation_outcome(engine, parsed, "failed")
        return True
    finally:
        if owned:
            engine.dispose()


def _describe(exc: BaseException) -> str:
    """``TypeError: ...`` — the class name is half the diagnosis."""
    return f"{type(exc).__name__}: {exc}"


def _parse_run_id(run_id: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(run_id))
    except (AttributeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Numeric coercion
# ---------------------------------------------------------------------------


def _decimal(value: Any, limit: Decimal) -> Decimal | None:
    """Coerce to a Decimal the column can hold, or None if it cannot.

    Money and ratios are NUMERIC, not FLOAT, so every float has to be converted
    — and ``Decimal(str(x))`` rather than ``Decimal(x)`` so 0.1 stores as 0.1
    instead of its binary expansion. NaN and infinity become None: they are the
    absence of an answer, and a stored NaN poisons every average computed over
    the column later.
    """
    if value is None:
        return None
    try:
        converted = Decimal(str(float(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not converted.is_finite() or abs(converted) >= limit:
        return None
    return converted


def _money(value: Any) -> Decimal | None:
    return _decimal(value, _MONEY_LIMIT)


def _ratio(value: Any) -> Decimal | None:
    return _decimal(value, _RATIO_LIMIT)


def _money_required(value: Any, field: str) -> Decimal:
    return _required(_money(value), value, field)


def _ratio_required(value: Any, field: str) -> Decimal:
    return _required(_ratio(value), value, field)


def _required(converted: Decimal | None, original: Any, field: str) -> Decimal:
    """For NOT NULL columns: refuse the row rather than invent a zero.

    A zero here would be indistinguishable from a real zero — a trade that
    broke even, a flat point on the curve. Raising sends the run to ``failed``
    with the field named, which is a thing someone can fix.
    """
    if converted is None:
        raise ValueError(f"{field} is not a storable number ({original!r})")
    return converted
