"""Market-data coverage endpoint.

Exists so the run form can offer a window that has prices in it. Coverage ends
weeks behind the calendar, so a date picker defaulting to "last 30 days"
produces an empty window, and the run fails for a reason that has nothing to do
with the student's strategy.

Read-only against ``public.market_data``, which the live trading system owns.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from src.schemas.market_data import CoverageResponse
from src.services import market_data as market_data_service

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(
    tickers: str | None = Query(
        default=None,
        description="Comma-separated tickers, e.g. 'AAPL,MSFT'.",
    ),
    strategy_key: str | None = Query(
        default=None,
        alias="strategyKey",
        description="Use this strategy's universe instead of an explicit list.",
    ),
) -> CoverageResponse:
    """Which dates this application has prices for.

    Takes either an explicit ticker list or a strategy key, whose universe is
    read from the registry. ``strategyKey`` is what the run form sends: the
    student picks a strategy, and the picker is then bounded by exactly the
    tickers that strategy trades.

    ``start`` and ``end`` are the window safe for the whole set. They are null,
    with the offending tickers in ``missing``, when any ticker has no bars,
    because there is then no window that covers the universe.
    """
    if (tickers is None) == (strategy_key is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pass exactly one of 'tickers' or 'strategyKey'.",
        )

    if strategy_key is not None:
        try:
            wanted = await market_data_service.universe_for_strategy(strategy_key)
        except market_data_service.UnknownStrategyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No strategy with key {strategy_key!r}.",
            ) from None
    else:
        wanted = [part.strip() for part in (tickers or "").split(",") if part.strip()]

    if not wanted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No tickers to report coverage for.",
        )

    return await market_data_service.coverage_for(wanted)
