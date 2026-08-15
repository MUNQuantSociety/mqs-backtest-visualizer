"""Backtest endpoints.

Query parameters are camelCase because the client sends its filter object
straight through as query params — ``strategyId``, not ``strategy_id``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status

from src.schemas.backtests import BacktestDetail, BacktestListResponse, BacktestStatus
from src.schemas.common import paginate
from src.services import sample_data

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("", response_model=BacktestListResponse)
async def list_backtests(
    search: str | None = Query(default=None),
    status_filter: BacktestStatus | None = Query(default=None, alias="status"),
    strategy_id: str | None = Query(default=None, alias="strategyId"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
) -> BacktestListResponse:
    items = sample_data.list_backtests()

    if status_filter is not None:
        items = [item for item in items if item.status == status_filter]
    if strategy_id is not None:
        items = [item for item in items if item.strategy_id == strategy_id]
    if search:
        needle = search.lower()
        items = [
            item
            for item in items
            if needle in f"{item.name} {item.symbol} {item.strategy_name}".lower()
        ]

    page_items, total = paginate(items, page, page_size)
    return BacktestListResponse(
        items=page_items, total=total, page=page, page_size=page_size
    )


@router.get("/{backtest_id}", response_model=BacktestDetail)
async def get_backtest(backtest_id: str) -> BacktestDetail:
    detail = sample_data.get_backtest(backtest_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    return detail


@router.delete("/{backtest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backtest(backtest_id: str) -> Response:
    if sample_data.get_backtest(backtest_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    # Deletion is not persisted yet — the sample data is regenerated per request.
    # The status code and error path are real so the client's mutation and cache
    # invalidation can be wired up against them now.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
