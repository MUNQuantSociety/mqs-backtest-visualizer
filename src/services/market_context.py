"""Dashboard market context: scored news and per-ticker indicators.

News and sentiment always come from ``public.news_sentiment`` on the live MQS
database (NEWS_POSTGRES_*), through read-only sessions, whichever database
POSTGRES_* points at. Prices come from ``public.market_data`` on the app's own
database. Neither table is ever written here.

Every window is anchored on the ticker's last stored bar, never on today:
market data ends weeks behind the calendar, and a window computed from ``now()``
would hold no closes. Sentiment uses the same anchor, so a gauge never counts
news published after the prices it sits beside.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import NewsDatabaseNotConfigured, news_session_scope, session_scope
from src.repositories import market_data as market_data_repo
from src.repositories import news_sentiment as news_repo
from src.repositories.news_sentiment import ArticleRow
from src.schemas.market_context import (
    IndicatorsResponse,
    NewsArticle,
    NewsResponse,
    TickerIndicators,
)
from src.services import technical_indicators as ta
from src.services.market_data import normalize_tickers

logger = logging.getLogger(__name__)

NEWS_LIMIT_MAX = 50
HEADLINE_MAX_CHARS = 140
UNKNOWN_SOURCE = "Unknown"

# Calendar days of bars to read: comfortably more than the 200 sessions the SMA
# regime needs (about 290 NYSE sessions fit in 420 calendar days).
_PRICE_LOOKBACK = timedelta(days=420)
_EXCHANGE_TZ = ZoneInfo("America/New_York")
_SESSION_CLOSE = time(16, 0)
_ELLIPSIS = "…"


class MarketContextUnavailable(RuntimeError):
    """A database could not answer; the route turns this into a retryable 503."""


# What failing to reach either database looks like. Configuration is included:
# a blank NEWS_POSTGRES_* block is reported the same way, never answered from
# another database.
_DATABASE_ERRORS = (SQLAlchemyError, OSError, NewsDatabaseNotConfigured)


def _source_from_url(url: str | None) -> str:
    """The publisher's host without ``www.``, which is all the table records of it."""
    host = urlparse(url or "").hostname or ""
    host = host.removeprefix("www.")
    return host or UNKNOWN_SOURCE


def _headline_from_summary(summary: str | None, fallback: str) -> str:
    """The start of ``content_summary``, cut at a word boundary.

    The pipeline stores title and body joined, so the opening characters are
    the title; where it ends is not recorded.
    """
    text = " ".join((summary or "").split())
    if not text:
        return fallback
    if len(text) <= HEADLINE_MAX_CHARS:
        return text
    cut = text[: HEADLINE_MAX_CHARS - len(_ELLIPSIS)]
    word_end = cut.rfind(" ")
    if word_end > 0:
        cut = cut[:word_end]
    return cut.rstrip(" ,;:-") + _ELLIPSIS


def to_news_article(row: ArticleRow) -> NewsArticle:
    """Shape a stored row for the dashboard's news list."""
    source = _source_from_url(row.article_url)
    return NewsArticle(
        id=str(row.id),
        source=source,
        published_at=row.published_at.replace(tzinfo=timezone.utc).isoformat(),
        headline=_headline_from_summary(row.content_summary, fallback=source),
        tickers=[row.ticker.strip().upper()],
        score=min(max(row.sentiment_score, -1.0), 1.0),
    )


async def latest_news(tickers: list[str] | None, limit: int) -> NewsResponse:
    """The newest scored articles from the live table, for ``tickers`` or all.

    Args:
        tickers: Symbols to restrict to (validated here), or None for all.
        limit: 1 to ``NEWS_LIMIT_MAX`` articles.

    Raises:
        ValueError: invalid tickers or limit.
        MarketContextUnavailable: the live news database could not be read.
    """
    if not 1 <= limit <= NEWS_LIMIT_MAX:
        raise ValueError(f"limit must be between 1 and {NEWS_LIMIT_MAX}.")
    wanted = None if tickers is None else normalize_tickers(tickers)
    try:
        async with news_session_scope() as session:
            rows = await news_repo.latest_articles(session, wanted, limit)
    except _DATABASE_ERRORS as exc:
        logger.error("News query failed: %s", _describe(exc))
        raise MarketContextUnavailable("News is unavailable. Please try again later.") from exc
    return NewsResponse(items=[to_news_article(row) for row in rows])


def _describe(exc: Exception) -> str:
    """What to log: the configuration message in full, a driver error by type only.

    Driver messages can carry connection details; the type says enough to act.
    """
    return str(exc) if isinstance(exc, NewsDatabaseNotConfigured) else type(exc).__name__


def _session_close_utc(session_date: date) -> datetime:
    """16:00 New York on ``session_date`` as naive UTC, matching ``published_at``."""
    close = datetime.combine(session_date, _SESSION_CLOSE, tzinfo=_EXCHANGE_TZ)
    return close.astimezone(timezone.utc).replace(tzinfo=None)


def build_indicators(
    ticker: str,
    closes: list[tuple[date, float]],
    articles: list[tuple[datetime, float]],
) -> TickerIndicators | None:
    """Indicators at the last close, or None when there are too few closes.

    Args:
        ticker: The symbol the row is for.
        closes: ``(session_date, close)`` pairs, oldest first.
        articles: ``(published_at, score)`` pairs, naive UTC, any order.
    """
    if len(closes) < ta.MIN_CLOSES:
        return None
    prices = [close for _, close in closes]
    as_of = closes[-1][0]
    sentiment, sentiment_delta = ta.sentiment_window_scores(
        articles, _session_close_utc(as_of)
    )
    return TickerIndicators(
        ticker=ticker,
        last=prices[-1],
        rsi14=ta.relative_strength_index(prices),
        macd_histogram=ta.macd_histogram(prices),
        sma_regime=ta.sma_regime(prices),
        momentum20d=ta.momentum(prices),
        sentiment7d=sentiment,
        sentiment_delta7d=sentiment_delta,
        as_of=as_of.isoformat(),
    )


async def _closes_for(
    session: AsyncSession, ticker: str
) -> list[tuple[date, float]] | None:
    """The ticker's recent session closes, or None when too few to use."""
    last_date = await market_data_repo.last_bar_date(session, ticker)
    if last_date is None:
        return None
    since = datetime.combine(last_date - _PRICE_LOOKBACK, time.min, tzinfo=_EXCHANGE_TZ)
    closes = await market_data_repo.daily_closes(session, ticker, since)
    return closes if len(closes) >= ta.MIN_CLOSES else None


async def indicators_for(tickers: list[str]) -> IndicatorsResponse:
    """Indicators for each ticker that has at least 200 sessions of closes.

    Prices are read first, from the app's database; sentiment is then read from
    the live news database for only the tickers that made it. A ticker with too
    little history is left out rather than padded: the schema has no field for
    "not computable", and invented numbers would be worse than a missing row.

    Raises:
        ValueError: invalid tickers.
        MarketContextUnavailable: either database could not be read.
    """
    wanted = list(dict.fromkeys(normalize_tickers(tickers)))
    try:
        async with session_scope() as session:
            closes_by_ticker = {ticker: await _closes_for(session, ticker) for ticker in wanted}
        usable = {ticker: closes for ticker, closes in closes_by_ticker.items() if closes}
        articles_by_ticker: dict[str, list[tuple[datetime, float]]] = {}
        if usable:
            async with news_session_scope() as news_session:
                for ticker, closes in usable.items():
                    window_end = _session_close_utc(closes[-1][0])
                    articles_by_ticker[ticker] = await news_repo.scores_between(
                        news_session, ticker, window_end - 2 * ta.SENTIMENT_WINDOW, window_end
                    )
    except _DATABASE_ERRORS as exc:
        logger.error("Indicators query failed: %s", _describe(exc))
        raise MarketContextUnavailable(
            "Indicators are unavailable. Please try again later."
        ) from exc

    items: list[TickerIndicators] = []
    for ticker in wanted:
        if ticker not in usable:
            logger.info("No indicators for %s: not enough price history", ticker)
            continue
        try:
            items.append(build_indicators(ticker, usable[ticker], articles_by_ticker[ticker]))
        except ValueError as exc:
            # A zero close in the momentum base: the row cannot be computed honestly.
            logger.warning("Skipping indicators for %s: %s", ticker, exc)
    return IndicatorsResponse(items=items)
