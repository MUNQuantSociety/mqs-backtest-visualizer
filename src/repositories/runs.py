"""Database access for backtest runs and their results.

The list query joins the strategy only for its display name; everything else
the list view renders is denormalised onto the run row, so paging through
hundreds of runs never touches ``run_equity_points`` or ``run_trades``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import BacktestRun, Strategy

# Statuses a run can no longer leave. Deleting one of these is a real delete;
# deleting anything else is a cancellation request.
TERMINAL_STATUSES = frozenset({"completed", "failed"})

# ``error_message`` is read by a human in a browser, and the worker truncates
# to the same length for the same reason.
ERROR_MESSAGE_LIMIT = 2000


@dataclass(frozen=True)
class RunFilters:
    """The list endpoint's query parameters, as the repository sees them."""

    search: str | None = None
    status: str | None = None
    strategy_key: str | None = None
    # Validation runs are hidden by default: they belong to the upload flow,
    # not to the student's list of experiments. ``None`` means "no filter".
    purpose: str | None = "user"
    owner_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RunListRow:
    """A run plus the strategy name the list view labels it with."""

    run: BacktestRun
    strategy_name: str


def for_user(statement, owner_id: uuid.UUID | None):
    """Owner-scoping seam — a no-op until authentication lands.

    When it does, this becomes ``statement.where(BacktestRun.owner_id ==
    owner_id)`` and every read path is scoped at once, because they all pass
    through here.
    """
    return statement


def parse_run_id(run_id: str) -> uuid.UUID | None:
    """Run ids are UUIDs; anything else is a 404, not a 500."""
    try:
        return uuid.UUID(str(run_id))
    except (ValueError, AttributeError, TypeError):
        return None


def _apply_filters(statement, filters: RunFilters):
    if filters.status:
        statement = statement.where(BacktestRun.status == filters.status)
    if filters.strategy_key:
        statement = statement.where(BacktestRun.strategy_key == filters.strategy_key)
    if filters.purpose:
        statement = statement.where(BacktestRun.purpose == filters.purpose)
    if filters.search:
        # The client's single search box covers everything the row displays.
        needle = f"%{filters.search.lower()}%"
        statement = statement.where(
            or_(
                func.lower(BacktestRun.name).like(needle),
                func.lower(BacktestRun.symbol).like(needle),
                func.lower(Strategy.name).like(needle),
            )
        )
    return for_user(statement, filters.owner_id)


async def list_runs(
    session: AsyncSession,
    filters: RunFilters,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[RunListRow], int]:
    """One page of runs, newest first, plus the total before slicing."""
    base = select(BacktestRun, Strategy.name).join(
        Strategy, Strategy.key == BacktestRun.strategy_key
    )
    filtered = _apply_filters(base, filters)

    count_statement = _apply_filters(
        select(func.count())
        .select_from(BacktestRun)
        .join(Strategy, Strategy.key == BacktestRun.strategy_key),
        filters,
    )
    total = int((await session.execute(count_statement)).scalar_one())

    offset = max(page - 1, 0) * page_size
    result = await session.execute(
        filtered.order_by(BacktestRun.created_at.desc(), BacktestRun.id)
        .offset(offset)
        .limit(page_size)
    )
    rows = [
        RunListRow(run=run, strategy_name=strategy_name)
        for run, strategy_name in result.all()
    ]
    return rows, total


async def get_run(
    session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> RunListRow | None:
    """One run with its metrics, equity curve, and trades eagerly loaded.

    ``selectinload`` rather than a join: the curve and the trade list are
    independent one-to-many collections, and joining both would multiply the
    result set by their product before the ORM de-duplicated it.
    """
    statement = (
        select(BacktestRun, Strategy.name)
        .join(Strategy, Strategy.key == BacktestRun.strategy_key)
        .where(BacktestRun.id == run_id)
        .options(
            selectinload(BacktestRun.metrics),
            selectinload(BacktestRun.equity_points),
            selectinload(BacktestRun.trades),
        )
    )
    row = (await session.execute(for_user(statement, owner_id))).first()
    if row is None:
        return None
    run, strategy_name = row
    return RunListRow(run=run, strategy_name=strategy_name)


async def create_run(
    session: AsyncSession,
    *,
    name: str,
    strategy_key: str,
    start_date: date,
    end_date: date,
    initial_capital: Decimal | float,
    symbol: str,
    engine_version: str,
    params: dict[str, Any] | None = None,
    timeframe: str = "1d",
    purpose: str = "user",
    owner_id: uuid.UUID | None = None,
) -> BacktestRun:
    """Insert a queued run and return it with its generated id populated."""
    run = BacktestRun(
        name=name,
        strategy_key=strategy_key,
        status="queued",
        params=params or {},
        start_date=start_date,
        end_date=end_date,
        timeframe=timeframe,
        symbol=symbol,
        initial_capital=Decimal(str(initial_capital)),
        progress_pct=0,
        engine_version=engine_version,
        purpose=purpose,
        owner_id=owner_id,
    )
    session.add(run)
    await session.flush()
    return run


async def delete_run(
    session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> bool:
    """Delete a run and everything it produced. False if it did not exist.

    Metrics, equity points, and trades go with it via ``ON DELETE CASCADE``
    plus the ORM's delete-orphan cascade — a run's results have no meaning
    without the run.
    """
    statement = for_user(select(BacktestRun).where(BacktestRun.id == run_id), owner_id)
    run = (await session.execute(statement)).scalar_one_or_none()
    if run is None:
        return False
    await session.delete(run)
    return True


async def delete_unclaimed_run(
    session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> bool:
    """Delete a run only while it is still queued. False if that is no longer true.

    The ``status = 'queued'`` predicate is the whole point: a worker claims a
    run with ``UPDATE ... WHERE id = :id AND status = 'queued'``, so this
    statement and that one contend for the same row and exactly one wins. If
    the worker won, nothing is deleted and the caller falls back to requesting
    cancellation; if this won, the claim matches zero rows and the worker walks
    away. No window exists in which a running worker loses its row.
    """
    statement = for_user(
        delete(BacktestRun).where(
            BacktestRun.id == run_id, BacktestRun.status == "queued"
        ),
        owner_id,
    )
    result = await session.execute(
        statement.execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def fail_unclaimed_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    message: str,
    owner_id: uuid.UUID | None = None,
) -> bool:
    """Mark a still-queued run failed. False if a worker already claimed it.

    Written for the one case the student cannot otherwise see: the run row was
    inserted but the job manager refused it, so nothing will ever pick it up
    and the row would sit in ``queued`` looking like a busy queue forever.

    The ``status = 'queued'`` predicate is the same one the worker's claim
    carries, so if the pool actually did accept the job in the moment the
    submitting request thought it had not, this matches nothing and the real
    run proceeds untouched.
    """
    statement = for_user(
        update(BacktestRun)
        .where(BacktestRun.id == run_id, BacktestRun.status == "queued")
        .values(
            status="failed",
            error_message=message[:ERROR_MESSAGE_LIMIT],
            finished_at=func.now(),
        ),
        owner_id,
    )
    result = await session.execute(
        statement.execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def request_cancel(
    session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> bool:
    """Ask a queued or running run to stop. False if there is no such run.

    Cancellation is cooperative: the worker polls this flag between timestamp
    groups. There is no way to signal a process-pool worker directly, and
    killing it would leave the run row claimed forever.
    """
    statement = for_user(select(BacktestRun).where(BacktestRun.id == run_id), owner_id)
    run = (await session.execute(statement)).scalar_one_or_none()
    if run is None:
        return False
    run.cancel_requested = True
    return True
