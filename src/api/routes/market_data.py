"""Market-data coverage endpoint.

Bounds the run form to actual FMP or database history for the selected tickers.
Provider errors are reported separately from tickers with no history.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.dependencies.current_user import require_current_user
from src.schemas.market_data import (
    CoverageResponse,
    SymbolSearchResponse,
    TickerClosesResponse,
    TickerValidationResponse,
)
from src.services import market_data as market_data_service
from src.services.market_data import FMPUnavailable, MarketDataUnavailable

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/validate-tickers", response_model=TickerValidationResponse)
async def validate_tickers(
    tickers: str = Query(max_length=1100, description="1 to 50 comma-separated FMP ticker symbols."),
    _owner_id: uuid.UUID = Depends(require_current_user),
) -> TickerValidationResponse:
    """Check exact FMP symbols before adding them to a backtest universe."""
    try:
        return await market_data_service.validate_tickers(tickers.split(","))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FMPUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/search-symbols", response_model=SymbolSearchResponse)
async def search_symbols(
    query: str = Query(min_length=1, max_length=20, description="The start of an FMP ticker symbol."),
    _owner_id: uuid.UUID = Depends(require_current_user),
) -> SymbolSearchResponse:
    """Offer symbols while a ticker is being typed.

    Suggestions only: a chosen symbol still goes through ``validate-tickers``
    before it joins a universe, so this endpoint never has to be right, just
    helpful. Gated like the validation it feeds — it spends provider quota.
    """
    try:
        return await market_data_service.search_symbols(query)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FMPUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/closes", response_model=TickerClosesResponse)
async def get_closes(
    ticker: str = Query(max_length=20, description="One ticker symbol, e.g. SPY."),
    start: date = Query(description="First New York trading date, YYYY-MM-DD."),
    end: date = Query(description="Last New York trading date, YYYY-MM-DD (inclusive)."),
    _owner_id: uuid.UUID = Depends(require_current_user),
) -> TickerClosesResponse:
    """A ticker's daily session closes, oldest first: the dashboard's benchmark line.

    Read from the market-data store, not a provider, so it spends no quota. A
    ticker with no closes in the window answers with no points.
    """
    try:
        return await market_data_service.closes_between(ticker, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


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

    try:
        return await market_data_service.coverage_for(wanted)
    except FMPUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
