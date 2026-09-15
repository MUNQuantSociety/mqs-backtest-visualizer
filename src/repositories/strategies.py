"""Database access for the shared strategy registry, without private report activity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Strategy

@dataclass(frozen=True)
class StrategyRow:
    """A registry row plus the aggregates the catalogue renders beside it."""

    strategy: Strategy
    run_count: int
    best_sharpe: Decimal | None
    best_return: Decimal | None
    last_run_at: datetime | None


def for_user(statement, owner_id: uuid.UUID | None):
    """Strategy metadata is shared; private report data is never joined here."""
    return statement


async def list_strategies(
    session: AsyncSession,
    *,
    include_disabled: bool = False,
    owner_id: uuid.UUID | None = None,
) -> list[StrategyRow]:
    """Every shared strategy the catalogue should show."""
    statement = _catalogue_statement().order_by(Strategy.key)
    if not include_disabled:
        statement = statement.where(Strategy.enabled.is_(True))

    result = await session.execute(for_user(statement, owner_id))
    return [_to_row(record) for record in result.all()]


async def get_strategy_row(
    session: AsyncSession, key: str, *, owner_id: uuid.UUID | None = None
) -> StrategyRow | None:
    """One strategy with its aggregates, whatever its lifecycle state.

    Deliberately ignores ``enabled``: this is how a student watches an upload
    that is still validating, or reads why one failed — both of which the
    catalogue hides on purpose.
    """
    statement = _catalogue_statement().where(Strategy.key == key)
    record = (await session.execute(for_user(statement, owner_id))).one_or_none()
    return _to_row(record) if record is not None else None


def _catalogue_statement():
    """Keep the public wire shape without exposing any owner's report activity."""
    return select(Strategy, literal(0), literal(None), literal(None), literal(None))


def _to_row(record) -> StrategyRow:
    strategy, run_count, best_sharpe, best_return, last_run_at = record
    return StrategyRow(
        strategy=strategy,
        run_count=int(run_count),
        best_sharpe=best_sharpe,
        best_return=best_return,
        last_run_at=last_run_at,
    )


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
    # Set only for a fragment-authored strategy; NULL means "a whole file".
    authoring: dict | None = None,
    owner_id: uuid.UUID | None = None,
) -> Strategy:
    """Insert a registry row and return it, flushed so the key is usable."""
    strategy = Strategy(
        key=key,
        owner_id=owner_id,
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
        authoring=authoring,
    )
    session.add(strategy)
    await session.flush()
    return strategy


async def delete_strategy(session: AsyncSession, key: str, *, owner_id: uuid.UUID) -> bool:
    """Remove one of ``owner_id``'s rows. False when there is no such row of theirs.

    Scoped by owner in the query, not checked afterwards: another member's
    strategy and an unknown key are the same answer, so a probe cannot tell
    them apart. The built-ins have no owner and therefore never match — the
    shared catalogue cannot be emptied through this path.

    Runs hold a ``RESTRICT`` foreign key to the strategy, so this raises rather
    than orphaning history — deleting a strategy someone has backtested is a
    product decision, not something a cleanup path should do silently.
    """
    statement = select(Strategy).where(Strategy.key == key, Strategy.owner_id == owner_id)
    strategy = (await session.execute(statement)).scalar_one_or_none()
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


async def attach_validation_run(session: AsyncSession, key: str, run_id: uuid.UUID) -> None:
    """Attach a run without resetting a verdict a fast worker already wrote."""
    await session.execute(
        update(Strategy)
        .where(Strategy.key == key, Strategy.kind == "user",
               (Strategy.validation_job_id.is_(None)) | (Strategy.validation_job_id == run_id))
        .values(validation_job_id=run_id)
    )


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
