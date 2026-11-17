"""Chart drawings saved per member and ticker, for the Build page's Draw menu.

The chart does all of the drawing; this keeps the result, so a member's
trend lines and notes follow them to another browser or device.
"""

from __future__ import annotations

import logging
import uuid

from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import chart_drawings as drawings_repo
from src.schemas.chart_drawings import ChartDrawing, ChartDrawingsResponse
from src.services.market_data import normalize_tickers

logger = logging.getLogger(__name__)


def _normalize_ticker(ticker: str) -> str:
    """One upper-case symbol, checked like every other ticker in the API."""
    return normalize_tickers([ticker])[0]


def _response(ticker: str, row) -> ChartDrawingsResponse:
    if row is None:
        return ChartDrawingsResponse(ticker=ticker, drawings=[])
    return ChartDrawingsResponse(
        ticker=ticker,
        # Validated again on the way out: a row is only ever written from a
        # validated list, so this costs little and keeps the response honest.
        drawings=[ChartDrawing.model_validate(item) for item in row.drawings],
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


async def get_drawings(owner_id: uuid.UUID, ticker: str) -> ChartDrawingsResponse:
    """The member's drawings for ``ticker``.

    Raises:
        ValueError: ``ticker`` is not a ticker symbol.
    """
    wanted = _normalize_ticker(ticker)
    await ensure_schema()
    async with session_scope() as session:
        row = await drawings_repo.get_drawings(session, owner_id=owner_id, ticker=wanted)
    return _response(wanted, row)


async def save_drawings(
    owner_id: uuid.UUID, ticker: str, drawings: list[ChartDrawing]
) -> ChartDrawingsResponse:
    """Replace the member's drawings for ``ticker`` with ``drawings``.

    Raises:
        ValueError: ``ticker`` is not a ticker symbol.
    """
    wanted = _normalize_ticker(ticker)
    payload = [drawing.model_dump(mode="json", by_alias=True, exclude_none=True) for drawing in drawings]
    await ensure_schema()
    async with session_scope() as session:
        row = await drawings_repo.replace_drawings(
            session, owner_id=owner_id, ticker=wanted, drawings=payload
        )
    logger.info("DRAWINGS | Saved %d drawing(s) for %s", len(payload), wanted)
    return _response(wanted, row)
