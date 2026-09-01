"""Proving an upload works by running it.

A validation run is not a separate code path. It is a row in
``app.backtest_runs`` with ``purpose='validation'``, submitted to the same job
manager and executed by the same worker. The student can open it like any other
run, and the worker flips the strategy to ``active`` when it passes. Anything
else would mean two run pipelines and one of them breaking silently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from engine import ENGINE_VERSION
from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import market_data as market_data_repo
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.repositories.runs import TERMINAL_STATUSES
from src.schemas.backtests import BacktestStatus, BacktestSummary
from src.services.backtests import (
    MODE_KEY,
    _dispatch,
    _symbol_for,
    create_backtest_run,
)
from src.services.strategy_validation.packaging import build_config, store_strategy_source

logger = logging.getLogger(__name__)


class ValidationStartError(RuntimeError):
    """Validation could not be started. A server problem, not a bad upload.

    Kept apart from :class:`StrategyValidationError` because the upload itself
    was fine: the source is stored and the row exists, and the student is told
    it could not be validated yet rather than that their code is wrong.
    """


async def validation_window(tickers: list[str]) -> tuple[date, date]:
    """The short window a validation run executes over.

    Anchored on the last bar the universe actually has, never on today: market
    data ends weeks behind the calendar, so a window computed from ``now()``
    returns zero rows and would fail every upload for a reason that has nothing
    to do with the uploaded code.
    """
    async with session_scope() as session:
        latest = await market_data_repo.latest_market_data_date(session, tickers)

    if latest is None:
        raise ValidationStartError(
            "there is no market data for "
            f"{', '.join(tickers)}, so there is no window to validate over"
        )

    span = max(int(settings.validation_window_days), 1)
    return latest - timedelta(days=span), latest


async def start_validation(
    *, strategy_key_value: str, strategy_name: str, tickers: list[str]
) -> BacktestSummary:
    """Queue the backtest that proves an upload works.

    An ordinary run in every respect except ``purpose='validation'``, which is
    what tells the worker to write the outcome back onto the strategy row and
    keeps it out of the catalogue's run aggregates.

    ``submit_backtest_run`` is not reused because it hardcodes
    ``purpose='user'`` and validates a client-supplied request; there is no
    client request here. The dispatch half *is* reused, because a refused
    worker pool has to be handled identically either way.
    """
    start, end = await validation_window(tickers)

    summary = await create_backtest_run(
        name=f"Validation: {strategy_name}"[:120],
        strategy_key=strategy_key_value,
        start_date=start,
        end_date=end,
        initial_capital=float(settings.validation_initial_capital),
        symbol=_symbol_for(tickers),
        engine_version=ENGINE_VERSION,
        # Event mode only: it is the dependable path, and a vectorized
        # approximation would prove nothing about the code the student wrote.
        params={MODE_KEY: "event"},
        purpose="validation",
    )

    dispatched = await _dispatch(summary)
    if dispatched.status is BacktestStatus.FAILED:
        raise ValidationStartError(
            "the worker pool would not accept the validation run; "
            "see the run for the reason"
        )

    _schedule_timeout(dispatched.id)
    return dispatched


# Strong references to the watchdogs in flight. ``asyncio`` keeps only weak
# references to tasks, so a timer nobody holds can be garbage collected
# mid-sleep and silently never fire.
_watchdogs: set[asyncio.Task] = set()


def _schedule_timeout(run_id: str) -> None:
    """Arm the wall-clock backstop for one validation run.

    Honest about what this is: a timer in the API process that sets the same
    ``cancel_requested`` flag a student's Cancel button sets. If the API
    restarts, the timer is gone and the run keeps going until the worker
    finishes with it. The startup reconciler cleans up after that. It
    is not a resource limit, and it cannot stop code that never returns to the
    engine's loop; only process isolation can do either.
    """
    timeout = float(settings.validation_timeout_seconds)
    if timeout <= 0:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - only outside the API process
        logger.warning(
            "No event loop to arm the validation timeout for run %s on", run_id
        )
        return

    task = loop.create_task(_cancel_when_overdue(run_id, timeout))
    _watchdogs.add(task)
    task.add_done_callback(_watchdogs.discard)


async def _cancel_when_overdue(run_id: str, timeout: float) -> None:
    """Sleep out the timeout, then ask an unfinished validation run to stop."""
    try:
        await asyncio.sleep(timeout)
        parsed = runs_repo.parse_run_id(run_id)
        if parsed is None:  # pragma: no cover - the id came from a created row
            return

        async with session_scope() as session:
            row = await runs_repo.get_run(session, parsed)
            if row is None or row.run.status in TERMINAL_STATUSES:
                return
            await runs_repo.request_cancel(session, parsed)

        logger.warning(
            "Validation run %s passed its %.0fs limit; cancellation requested",
            run_id,
            timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # The run is still going and will still finish; losing the backstop is
        # not worth an unhandled exception in a background task.
        logger.exception("Validation timeout for run %s could not be applied", run_id)


async def mark_validation_unstarted(key: str, reason: str) -> None:
    """Park an upload whose validation never got off the ground.

    Without this the strategy sits in ``validating`` forever, which reads to a
    student as "still working" for a run that does not exist.
    """
    try:
        async with session_scope() as session:
            await strategies_repo.set_validation_state(
                session, key, status="failed_validation", enabled=False
            )
    except Exception:
        logger.exception("Strategy %s could not be marked unvalidated (%s)", key, reason)


# ---------------------------------------------------------------------------
# Migration off the staging column
# ---------------------------------------------------------------------------


async def migrate_staged_sources() -> int:
    """Move any pre-store upload into the store. Returns how many moved.

    Before this pipeline existed, ``POST /strategies`` parked source in
    ``app.strategies.source_staging`` and did nothing with it. Such a row can
    never run: the worker loads uploads from the store and from nowhere else.
    This sweep copies the staged source into the store, points the row at it,
    and empties the column, after which the column stays empty forever,
    because nothing writes it any more.

    A validation run is *not* started for the migrated rows. They were uploaded
    before validation existed and their authors are not waiting on a result;
    starting a run each would mean an unbounded burst of backtests on the first
    upload after a deploy.
    """
    await ensure_schema()
    async with session_scope() as session:
        staged = await strategies_repo.strategies_with_staged_source(session)
        pending = [(row.key, row.source_staging or "") for row in staged]

    migrated = 0
    for key, source in pending:
        try:
            storage = store_strategy_source(key, source, build_config(key))
            async with session_scope() as session:
                await strategies_repo.adopt_staged_source(
                    session, key, storage_key=storage
                )
        except Exception:
            logger.exception("Staged source for strategy %s could not be migrated", key)
            continue
        migrated += 1

    if migrated:
        logger.info("Migrated %d staged strategy source(s) into the store", migrated)
    return migrated
