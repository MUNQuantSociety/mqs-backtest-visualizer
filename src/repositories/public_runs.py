"""Owner-scoped dashboard reads across current runs and public history.

The worker updates ``app.backtest_runs``, so those rows are read directly to
keep every lifecycle state visible. Public history can use summary columns
or a ``results`` JSONB object; normalizing whole rows supports both layouts
without changing either table. A current run wins when its id is also public.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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


_OWNED_RUNS = """
    WITH owned_runs AS (
        SELECT a.id, a.owner_id, a.created_at, to_jsonb(a) AS results
        FROM app.backtest_runs a
        WHERE a.owner_id = :owner_id AND a.purpose = 'user'

        UNION ALL

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
    )
"""

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

_LIST = text(
    f"""
    {_OWNED_RUNS}
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

_COUNT = text(f"{_OWNED_RUNS} SELECT count(*) {_FILTERS}")


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


async def list_runs_for_owner(
    session: AsyncSession,
    owner_id: uuid.UUID,
    filters: PublicRunFilters,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[PublicRunRow], int]:
    """One page of this user's current and historical runs, newest first."""
    offset = max(page - 1, 0) * page_size
    bind = _params(owner_id, filters, offset=offset, limit=page_size)
    total = int((await session.execute(_COUNT, bind)).scalar_one())
    result = await session.execute(_LIST, bind)
    return [_to_row(row) for row in result.mappings().all()], total
