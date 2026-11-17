"""Rows of ``app.chart_drawings``: one per member and ticker."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ChartDrawingSet


async def get_drawings(
    session: AsyncSession, *, owner_id: uuid.UUID, ticker: str
) -> ChartDrawingSet | None:
    statement = select(ChartDrawingSet).where(
        ChartDrawingSet.owner_id == owner_id, ChartDrawingSet.ticker == ticker
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def replace_drawings(
    session: AsyncSession, *, owner_id: uuid.UUID, ticker: str, drawings: list[dict]
) -> ChartDrawingSet | None:
    """Save ``drawings`` as the whole list; an empty list removes the row."""
    if not drawings:
        await session.execute(
            delete(ChartDrawingSet).where(
                ChartDrawingSet.owner_id == owner_id, ChartDrawingSet.ticker == ticker
            )
        )
        return None
    statement = (
        insert(ChartDrawingSet)
        .values(owner_id=owner_id, ticker=ticker, drawings=drawings)
        .on_conflict_do_update(
            index_elements=[ChartDrawingSet.owner_id, ChartDrawingSet.ticker],
            set_={"drawings": drawings, "updated_at": func.now()},
        )
        .returning(ChartDrawingSet)
    )
    return (await session.execute(statement)).scalar_one()
