"""Live portfolio endpoints (MQS Master views).

These describe the *live* trading system, which the visualizer only ever reads.
No route here writes to portfolio state.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from src.schemas.common import paginate
from src.schemas.portfolios import (
    CompositionSeries,
    CorrelationMatrix,
    EquitySeries,
    ExecutionListResponse,
    PortfolioDetail,
    PortfolioListResponse,
)
from src.services import sample_data

router = APIRouter(prefix="/live/portfolios", tags=["live-portfolios"])


def _not_found(portfolio_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No portfolio with id {portfolio_id!r}.",
    )


@router.get("", response_model=PortfolioListResponse)
async def list_portfolios() -> PortfolioListResponse:
    items = sample_data.list_portfolios()
    return PortfolioListResponse(
        items=items, total=len(items), page=1, page_size=len(items)
    )


@router.get("/{portfolio_id}", response_model=PortfolioDetail)
async def get_portfolio(portfolio_id: str) -> PortfolioDetail:
    detail = sample_data.get_portfolio(portfolio_id)
    if detail is None:
        raise _not_found(portfolio_id)
    return detail


@router.get("/{portfolio_id}/equity", response_model=EquitySeries)
async def get_equity(
    portfolio_id: str,
    days: int = Query(default=180, ge=1, le=3650),
) -> EquitySeries:
    if sample_data.get_portfolio(portfolio_id) is None:
        raise _not_found(portfolio_id)
    return sample_data.portfolio_equity(portfolio_id, days)


@router.get("/{portfolio_id}/composition", response_model=CompositionSeries)
async def get_composition(
    portfolio_id: str,
    days: int = Query(default=180, ge=1, le=3650),
) -> CompositionSeries:
    series = sample_data.portfolio_composition(portfolio_id, days)
    if series is None:
        raise _not_found(portfolio_id)
    return series


@router.get("/{portfolio_id}/executions", response_model=ExecutionListResponse)
async def get_executions(
    portfolio_id: str,
    ticker: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
) -> ExecutionListResponse:
    items = sample_data.portfolio_executions(portfolio_id)
    if items is None:
        raise _not_found(portfolio_id)

    if ticker:
        items = [item for item in items if item.ticker == ticker]

    page_items, total = paginate(items, page, page_size)
    return ExecutionListResponse(
        items=page_items, total=total, page=page, page_size=page_size
    )


@router.get("/{portfolio_id}/correlations", response_model=CorrelationMatrix)
async def get_correlations(portfolio_id: str) -> CorrelationMatrix:
    matrix = sample_data.portfolio_correlations(portfolio_id)
    if matrix is None:
        raise _not_found(portfolio_id)
    return matrix
