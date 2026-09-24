"""Dashboard market-context endpoints: ``GET /news`` and ``GET /indicators``.

Mounted at the API root because that is where the frontend's ``market-api.ts``
calls them. Both are read-only views over tables the live trading system owns.
A 503 is what the client treats as "try again", so a database failure is
reported as one, never as an empty list.
"""

from __future__ import annotations

from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.dependencies.current_user import require_current_user
from src.schemas.market_context import IndicatorsResponse, NewsResponse
from src.services import market_context as market_context_service
from src.services.market_context import NEWS_LIMIT_MAX, MarketContextUnavailable

router = APIRouter(tags=["market-context"])

_RETRY_AFTER_SECONDS = "30"


def _split_tickers(tickers: str) -> list[str]:
    return tickers.split(",")


def _unavailable(exc: MarketContextUnavailable) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
        headers={"Retry-After": _RETRY_AFTER_SECONDS},
    )


@router.get("/news", response_model=NewsResponse)
async def get_news(
    tickers: str | None = Query(
        default=None,
        max_length=1100,
        description="1 to 50 comma-separated tickers. Omit for news across every ticker.",
    ),
    limit: int = Query(default=8, ge=1, le=NEWS_LIMIT_MAX, description="Articles to return."),
    _owner_id: uuid.UUID = Depends(require_current_user),
) -> NewsResponse:
    """The newest model-scored articles, newest first.

    Each article is stored once per ticker, so ``tickers`` always holds one
    symbol. ``headline`` is the start of the stored summary; ``source`` is the
    publisher's host.
    """
    wanted = None if tickers is None else _split_tickers(tickers)
    try:
        return await market_context_service.latest_news(wanted, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketContextUnavailable as exc:
        raise _unavailable(exc) from None


@router.get("/indicators", response_model=IndicatorsResponse)
async def get_indicators(
    tickers: str = Query(max_length=1100, description="1 to 50 comma-separated tickers."),
    window: Literal["7d"] = Query(
        default="7d", description="Sentiment window. Only 7 days is supported."
    ),
    _owner_id: uuid.UUID = Depends(require_current_user),
) -> IndicatorsResponse:
    """Technicals at each ticker's last session close, with 7-day news sentiment.

    Tickers with fewer than 200 sessions of closes are omitted. A 7-day window
    with no articles scores 0.0.
    """
    try:
        return await market_context_service.indicators_for(_split_tickers(tickers))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketContextUnavailable as exc:
        raise _unavailable(exc) from None
