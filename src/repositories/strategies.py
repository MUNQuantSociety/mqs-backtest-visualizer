"""Database access for the strategy registry.

Every SQL statement about strategies lives here. The interesting part is the
aggregate query: the catalogue shows run count, best Sharpe, best return and
last-run time per strategy, and computing those by loading runs into Python
would mean reading every row of ``backtest_runs`` to render one page.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import BacktestRun, Strategy

@dataclass(frozen=True)
class StrategyRow:
    """A registry row plus the aggregates the catalogue renders beside it."""

    strategy: Strategy
    run_count: int
    best_sharpe: Decimal | None
    best_return: Decimal | None
    last_run_at: datetime | None


def _aggregate_subquery():
    """Per-strategy run statistics, computed by PostgreSQL in one pass.

    Validation runs are excluded: they are an implementation detail of the
    upload flow, and counting them would tell a student their strategy has been
    backtested once when they have never run it.
    """
    return (
        select(
            BacktestRun.strategy_key.label("strategy_key"),
            func.count().label("run_count"),
            func.max(BacktestRun.sharpe).label("best_sharpe"),
            func.max(BacktestRun.total_return).label("best_return"),
            func.max(BacktestRun.created_at).label("last_run_at"),
        )
        .where(BacktestRun.purpose == "user")
        .group_by(BacktestRun.strategy_key)
        .subquery()
    )


def for_user(statement, owner_id: uuid.UUID | None):
    """Owner-scoping seam.

    The registry is shared — every student sees every strategy — so this is a
    no-op today. It exists so the auth session has one obvious place to add a
    filter instead of hunting through call sites.
    """
    return statement


async def list_strategies(
    session: AsyncSession,
    *,
    include_disabled: bool = False,
    owner_id: uuid.UUID | None = None,
) -> list[StrategyRow]:
    """Every strategy the catalogue should show, newest aggregates included."""
    aggregates = _aggregate_subquery()
    statement = (
        select(
            Strategy,
            func.coalesce(aggregates.c.run_count, 0),
            aggregates.c.best_sharpe,
            aggregates.c.best_return,
            aggregates.c.last_run_at,
        )
        .outerjoin(aggregates, aggregates.c.strategy_key == Strategy.key)
        .order_by(Strategy.key)
    )
    if not include_disabled:
        statement = statement.where(Strategy.enabled.is_(True))

    result = await session.execute(for_user(statement, owner_id))
    return [
        StrategyRow(
            strategy=strategy,
            run_count=int(run_count),
            best_sharpe=best_sharpe,
            best_return=best_return,
            last_run_at=last_run_at,
        )
        for strategy, run_count, best_sharpe, best_return, last_run_at in result.all()
    ]


async def get_strategy(session: AsyncSession, key: str) -> Strategy | None:
    """One registry row by key, without aggregates."""
    return await session.get(Strategy, key)


async def create_strategy(
    session: AsyncSession,
    *,
    key: str,
    name: str,
    description: str,
    kind: str,
    status: str,
    enabled: bool,
    tags: list[str] | None = None,
    universe: list[str] | None = None,
    param_specs: list[dict] | None = None,
    class_path: str | None = None,
    storage_key: str | None = None,
    source_staging: str | None = None,
) -> Strategy:
    """Insert a registry row and return it, flushed so the key is usable."""
    strategy = Strategy(
        key=key,
        name=name,
        description=description,
        tags=tags or [],
        universe=universe or [],
        param_specs=param_specs or [],
        kind=kind,
        class_path=class_path,
        storage_key=storage_key,
        status=status,
        enabled=enabled,
        source_staging=source_staging,
    )
    session.add(strategy)
    await session.flush()
    return strategy


async def delete_strategy(session: AsyncSession, key: str) -> bool:
    """Remove a registry row. Returns False when there was nothing to remove.

    Runs hold a ``RESTRICT`` foreign key to the strategy, so this raises rather
    than orphaning history — deleting a strategy someone has backtested is a
    product decision, not something a cleanup path should do silently.
    """
    strategy = await session.get(Strategy, key)
    if strategy is None:
        return False
    await session.delete(strategy)
    return True


async def set_validation_state(
    session: AsyncSession,
    key: str,
    *,
    status: str,
    enabled: bool,
    validation_run_id: uuid.UUID | None = None,
) -> bool:
    """Move an upload through its lifecycle. False when the key is unknown.

    The API side uses this for the states it decides itself — an upload that
    never reached the worker at all. The passing/failing outcome of a
    validation run is written by the worker instead (``src/workers/run_job.py``),
    because that process is the only one that knows how the run ended.
    """
    strategy = await session.get(Strategy, key)
    if strategy is None:
        return False
    strategy.status = status
    strategy.enabled = enabled
    if validation_run_id is not None:
        strategy.validation_run_id = validation_run_id
    return True


async def strategies_with_staged_source(session: AsyncSession) -> list[Strategy]:
    """Uploads whose source is still in the staging column, not the store.

    ``source_staging`` was the placeholder for uploaded source before the
    strategy store existed. Rows written then cannot run — the worker loads a
    strategy from the store and nothing else — so they are swept into the store
    on the next upload and the column is emptied for good.
    """
    statement = select(Strategy).where(
        Strategy.source_staging.is_not(None), Strategy.kind == "user"
    )
    return list((await session.execute(statement)).scalars().all())


async def adopt_staged_source(
    session: AsyncSession, key: str, *, storage_key: str
) -> bool:
    """Point a migrated row at the store and drop its staged copy.

    Clearing ``source_staging`` in the same transaction that sets
    ``storage_key`` is what makes the migration safe to re-run: a row can never
    be half-migrated, so the sweep either has work to do or has none.
    """
    strategy = await session.get(Strategy, key)
    if strategy is None:
        return False
    strategy.storage_key = storage_key
    strategy.source_staging = None
    return True
