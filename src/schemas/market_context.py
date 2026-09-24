"""Dashboard market-context models: per-ticker indicators and scored news.

Mirrors ``Backtest_Visualiser_FE/src/features/market/types.ts``. The client parses
both payloads with Zod and has no fallback in production, so every key and bound
here is copied from those schemas, not chosen.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.schemas.common import CamelModel


class TickerIndicators(CamelModel):
    """Technicals at one session's close, plus the ticker's 7-day news sentiment."""

    ticker: str
    last: float
    rsi14: float = Field(ge=0, le=100)
    # MACD(12, 26, 9) histogram, in price units.
    macd_histogram: float
    sma_regime: Literal["above", "below"]
    # 20-session return as a ratio: 0.031 is +3.1%.
    # Explicit aliases on these three: the camelCase generator capitalises a
    # letter after a digit ("momentum20D"), which the client's schema rejects.
    momentum20d: float = Field(alias="momentum20d")
    sentiment7d: float = Field(ge=-1, le=1, alias="sentiment7d")
    sentiment_delta7d: float = Field(alias="sentimentDelta7d")
    # ISO date of the session the technicals were computed at the close of.
    as_of: str


class IndicatorsResponse(CamelModel):
    """Only tickers with enough price history appear; see the service for why."""

    items: list[TickerIndicators]


class NewsArticle(CamelModel):
    """One scored article."""

    id: str
    source: str
    # ISO 8601 with an explicit UTC offset.
    published_at: str
    headline: str
    # The stored text: title and body joined, cut by the pipeline at 1,000
    # characters. What the story card shows.
    summary: str
    # The publisher's page, or null when the stored link is not an http(s) URL.
    url: str | None = None
    tickers: list[str]
    score: float = Field(ge=-1, le=1)


class NewsStory(CamelModel):
    """One article's own title and summary paragraph, for its story card."""

    id: str
    title: str
    # The real summary paragraph, or null when neither the publisher's page nor
    # the stored text yields one separate from the title.
    summary: str | None = None
    # Where the summary came from: the publisher's page head, the stored text
    # with the publisher's title cut off it, or nowhere.
    origin: Literal["publisher", "stored", "none"]


class NewsResponse(CamelModel):
    """Newest first."""

    items: list[NewsArticle]
