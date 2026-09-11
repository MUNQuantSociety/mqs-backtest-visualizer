"""Dashboard list reads — ``public.backtest_runs`` scoped by owner.

Identity is columns (``id``, ``owner_id``, ``created_at``). Everything a
strategy produced sits in ``results`` JSONB.

Create, detail, and the worker still use ``app.backtest_runs``.
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


_FILTERS = """
    FROM public.backtest_runs r
    LEFT JOIN app.strategies s ON s.key = r.results->>'strategy_key'
    WHERE r.owner_id = :owner_id
      AND (CAST(:status AS text) IS NULL OR r.results->>'status' = CAST(:status AS text))
      AND (
            CAST(:strategy_key AS text) IS NULL
            OR r.results->>'strategy_key' = CAST(:strategy_key AS text)
          )
      AND (
            CAST(:search AS text) IS NULL
            OR lower(COALESCE(r.results->>'name', '')) LIKE CAST(:search AS text)
            OR lower(COALESCE(r.results->>'symbol', '')) LIKE CAST(:search AS text)
            OR lower(COALESCE(r.results->>'strategy_key', '')) LIKE CAST(:search AS text)
            OR lower(COALESCE(s.name, '')) LIKE CAST(:search AS text)
          )
"""

_LIST = text(
    f"""
    SELECT
        r.id,
        r.owner_id,
        r.created_at,
        r.results,
        COALESCE(s.name, r.results->>'strategy_key', '') AS strategy_name
    {_FILTERS}
    ORDER BY r.created_at DESC, r.id
    OFFSET :offset
    LIMIT :limit
    """
)

_COUNT = text(f"SELECT count(*) {_FILTERS}")


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
    """One page of this user's public runs, newest first."""
    offset = max(page - 1, 0) * page_size
    bind = _params(owner_id, filters, offset=offset, limit=page_size)
    total = int((await session.execute(_COUNT, bind)).scalar_one())
    result = await session.execute(_LIST, bind)
    return [_to_row(row) for row in result.mappings().all()], total
