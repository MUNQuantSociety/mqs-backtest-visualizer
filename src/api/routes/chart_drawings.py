"""Chart drawings for the Build page, saved per member and ticker."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies.current_user import require_current_user
from src.schemas.chart_drawings import ChartDrawingsResponse, ChartDrawingsUpdate
from src.services import chart_drawings as chart_drawings_service

router = APIRouter(prefix="/chart-drawings", tags=["chart-drawings"])


@router.get("/{ticker}", response_model=ChartDrawingsResponse)
async def get_chart_drawings(
    ticker: str,
    owner_id: uuid.UUID = Depends(require_current_user),
) -> ChartDrawingsResponse:
    """The signed-in member's drawings on ``ticker``'s chart; none is an empty list."""
    try:
        return await chart_drawings_service.get_drawings(owner_id, ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.put("/{ticker}", response_model=ChartDrawingsResponse)
async def put_chart_drawings(
    ticker: str,
    body: ChartDrawingsUpdate,
    owner_id: uuid.UUID = Depends(require_current_user),
) -> ChartDrawingsResponse:
    """Replace the member's drawings on ``ticker``'s chart with the list sent.

    The chart sends its whole list after every change, so the last save wins.
    An empty list clears the ticker.
    """
    try:
        return await chart_drawings_service.save_drawings(owner_id, ticker, body.drawings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
