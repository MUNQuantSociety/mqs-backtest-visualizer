"""Owner-scoped dashboard reads across current runs and public history.

The worker updates ``app.backtest_runs``, so those rows are read directly to
keep every lifecycle state visible. Public history can use summary columns
or a ``results`` JSONB object; normalizing whole rows supports both layouts
without changing either table. A current run wins when its id is also public.

``public.backtest_runs`` belongs to the warehouse, not to this app: nothing here
creates it and ``create_all`` only builds the ``app`` schema. A database that has
never seen the warehouse — every fresh Docker dev database — therefore does not
have it, and the history half of the union is a query against a table that is
not there. So it is optional: attempted once, and dropped for the rest of the
process when Postgres says it does not exist, instead of turning every dashboard
read into a 500.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicRunFilters:
    search: str | None = None
    status: str | None = None
    strategy_key: str | None = None


@dataclass(frozen=True)
class PublicRunRow:
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime
    results: dict[str, Any]
    strategy_name: str


_CURRENT_RUNS = """
        SELECT a.id, a.owner_id, a.created_at, to_jsonb(a) AS results
        FROM app.backtest_runs a
        WHERE a.owner_id = :owner_id AND a.purpose = 'user'
"""

_HISTORICAL_RUNS = """
        SELECT p.id, p.owner_id, p.created_at,
               CASE WHEN jsonb_typeof(to_jsonb(p)->'results') = 'object'
                    THEN to_jsonb(p)->'results'
                    ELSE to_jsonb(p)
               END AS results
        FROM public.backtest_runs p
        WHERE p.owner_id = :owner_id
          AND NOT EXISTS (
              SELECT 1 FROM app.backtest_runs a
              WHERE a.id = p.id AND a.owner_id = :owner_id AND a.purpose = 'user'
          )
"""


def _owned_runs(*, with_history: bool) -> str:
    body = (
        f"{_CURRENT_RUNS}\n        UNION ALL\n{_HISTORICAL_RUNS}"
        if with_history
        else _CURRENT_RUNS
    )
    return f"WITH owned_runs AS (\n{body}\n    )"

_STRATEGY_KEY = "COALESCE(r.results->>'strategy_key', r.results->>'strategyId')"

_FILTERS = f"""
    FROM owned_runs r
    LEFT JOIN app.strategies s ON s.key = {_STRATEGY_KEY}
    WHERE (CAST(:status AS text) IS NULL OR COALESCE(r.results->>'status', 'completed') = CAST(:status AS text))
      AND (CAST(:strategy_key AS text) IS NULL OR {_STRATEGY_KEY} = CAST(:strategy_key AS text))
      AND (
            CAST(:search AS text) IS NULL
            OR lower(COALESCE(r.results->>'name', '')) LIKE CAST(:search AS text)
            OR lower(COALESCE(r.results->>'symbol', '')) LIKE CAST(:search AS text)
            OR lower(COALESCE({_STRATEGY_KEY}, '')) LIKE CAST(:search AS text)
            OR lower(COALESCE(s.name, '')) LIKE CAST(:search AS text)
          )
"""

def _list_statement(*, with_history: bool) -> TextClause:
    return text(
        f"""
    {_owned_runs(with_history=with_history)}
    SELECT
        r.id,
        r.owner_id,
        r.created_at,
        r.results,
        COALESCE(s.name, {_STRATEGY_KEY}, '') AS strategy_name
    {_FILTERS}
    ORDER BY r.created_at DESC, r.id
    OFFSET :offset
    LIMIT :limit
    """
    )


def _count_statement(*, with_history: bool) -> TextClause:
    return text(f"{_owned_runs(with_history=with_history)} SELECT count(*) {_FILTERS}")


# Built once each: the two shapes are fixed, and only which pair gets used varies.
_LIST = _list_statement(with_history=True)
_COUNT = _count_statement(with_history=True)
_LIST_CURRENT_ONLY = _list_statement(with_history=False)
_COUNT_CURRENT_ONLY = _count_statement(with_history=False)

_HISTORY_RELATION = "public.backtest_runs"

# Asked once per process, before any statement that names the table.
#
# The check cannot live inside the listing query itself: Postgres resolves
# relation names while parsing, so `FROM public.backtest_runs WHERE
# to_regclass('public.backtest_runs') IS NOT NULL` still fails at the FROM —
# the WHERE never runs. `to_regclass` returns NULL instead of raising, which is
# exactly what makes it usable as a separate probe.
_HISTORY_PROBE = text("SELECT to_regclass(:relation) IS NOT NULL")

# Postgres: undefined_table. Checked by code rather than by driver exception
# class so this does not depend on asyncpg being the driver.
_UNDEFINED_TABLE = "42P01"

# None until probed. Process-local on purpose: a restart re-checks, so adding
# the table later needs no code change and no cache to invalidate.
_history_available: bool | None = None


def _is_missing_table(error: BaseException) -> bool:
    """True when Postgres refused the statement for naming a table it lacks."""
    seen: list[BaseException | None] = [error]
    candidate: BaseException | None = error
    for attribute in ("orig", "__cause__", "__context__"):
        candidate = getattr(candidate, attribute, None)
        seen.append(candidate)
    for item in seen:
        if item is None:
            continue
        if getattr(item, "sqlstate", None) == _UNDEFINED_TABLE:
            return True
        if getattr(item, "pgcode", None) == _UNDEFINED_TABLE:
            return True
        if type(item).__name__ == "UndefinedTableError":
            return True
    return False


def _params(
    owner_id: uuid.UUID, filters: PublicRunFilters, *, offset: int, limit: int
) -> dict:
    needle = f"%{filters.search.lower()}%" if filters.search else None
    return {
        "owner_id": owner_id,
        "status": filters.status,
        "strategy_key": filters.strategy_key,
        "search": needle,
        "offset": offset,
        "limit": limit,
    }


def _to_row(mapping) -> PublicRunRow:
    raw = mapping["results"]
    if raw is None:
        results: dict[str, Any] = {}
    elif isinstance(raw, dict):
        results = raw
    else:
        results = dict(raw)
    return PublicRunRow(
        id=mapping["id"],
        owner_id=mapping["owner_id"],
        created_at=mapping["created_at"],
        results=results,
        strategy_name=mapping["strategy_name"] or "",
    )


async def _history_is_present(session: AsyncSession) -> bool:
    """Whether the warehouse history table exists, asked at most once."""
    global _history_available

    if _history_available is None:
        result = await session.execute(_HISTORY_PROBE, {"relation": _HISTORY_RELATION})
        _history_available = bool(result.scalar_one())
        if _history_available:
            logger.info("%s found; dashboard lists include history", _HISTORY_RELATION)
        else:
            logger.warning(
                "%s does not exist on this database, so dashboard lists will show "
                "current runs from app.backtest_runs only. Expected when the "
                "warehouse is not attached, as on a fresh dev database.",
                _HISTORY_RELATION,
            )
    return _history_available


async def _read_page(
    session: AsyncSession,
    bind: dict,
    count: TextClause,
    listing: TextClause,
) -> tuple[list[PublicRunRow], int]:
    total = int((await session.execute(count, bind)).scalar_one())
    result = await session.execute(listing, bind)
    return [_to_row(row) for row in result.mappings().all()], total


async def list_runs_for_owner(
    session: AsyncSession,
    owner_id: uuid.UUID,
    filters: PublicRunFilters,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[PublicRunRow], int]:
    """One page of this user's current and historical runs, newest first.

    Reads current runs alone when ``public.backtest_runs`` does not exist. A
    user's own runs all live in ``app.backtest_runs``, so that is the complete
    answer on a database with no warehouse attached, not a truncated one.
    """
    global _history_available

    offset = max(page - 1, 0) * page_size
    bind = _params(owner_id, filters, offset=offset, limit=page_size)

    if await _history_is_present(session):
        try:
            return await _read_page(session, bind, _COUNT, _LIST)
        except DatabaseError as error:
            if not _is_missing_table(error):
                raise
            # The probe said the table was there, so this is a table dropped or
            # a grant revoked under a running process. Rare, worth the full
            # traceback, and still not worth failing a dashboard read over.
            logger.warning(
                "%s vanished between the existence check and the query; "
                "falling back to current runs only",
                _HISTORY_RELATION,
                exc_info=error,
            )
            _history_available = False
            # The failed statement aborted the transaction, so nothing else can
            # run on this session until it is rolled back.
            await session.rollback()

    return await _read_page(session, bind, _COUNT_CURRENT_ONLY, _LIST_CURRENT_ONLY)
