"""Read-only access to ``public.news_sentiment``.

The MQSMaster NLP pipeline owns this table: one row per article URL, scored by
the fine-tuned FinBERT model into ``sentiment_score`` in [-1, 1]. This
application only reads it, for each backtest's news panel and the dashboard's
sentiment gauges, and must never write to it. The table is a fixed historical
dataset, not a live feed, so news is always asked for by a run's dates.

``published_at`` is a naive timestamp holding UTC (the scrapers normalise to UTC
and drop the zone). ``content_summary`` is the article title and body joined and
truncated, so it starts with the headline but has no separate title column.

Rows with a NULL score or date are skipped: the dashboard cannot place or colour
them, and its schema rejects nulls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

_ARTICLE_COLUMNS = (
    "SELECT id, ticker, article_url, published_at, sentiment_score, content_summary "
    "FROM public.news_sentiment "
    "WHERE sentiment_score IS NOT NULL AND published_at IS NOT NULL "
)

# A backtest run's articles: its tickers, published inside its window, newest
# first. Served by the (ticker, published_at) index.
_ARTICLES_IN_WINDOW_SQL = text(
    _ARTICLE_COLUMNS
    + "AND ticker IN :tickers AND published_at >= :start AND published_at < :end "
    "ORDER BY published_at DESC, id DESC LIMIT :limit"
).bindparams(
    bindparam("tickers", expanding=True),
    bindparam("start"),
    bindparam("end"),
    bindparam("limit"),
)

_ARTICLE_BY_ID_SQL = text(_ARTICLE_COLUMNS + "AND id = :id").bindparams(bindparam("id"))

_SCORES_IN_RANGE_SQL = text(
    "SELECT published_at, sentiment_score FROM public.news_sentiment "
    "WHERE ticker = :ticker AND sentiment_score IS NOT NULL "
    "AND published_at > :start AND published_at <= :end"
).bindparams(bindparam("ticker"), bindparam("start"), bindparam("end"))


@dataclass(frozen=True)
class ArticleRow:
    """One scored article as stored. ``published_at`` is naive UTC."""

    id: int
    ticker: str
    article_url: str | None
    published_at: datetime
    sentiment_score: float
    content_summary: str | None


async def articles_between(
    session: AsyncSession,
    tickers: list[str],
    start: datetime,
    end: datetime,
    limit: int,
) -> list[ArticleRow]:
    """Up to ``limit`` articles for ``tickers`` published in ``[start, end)``, newest first.

    Args:
        session: An open async session.
        tickers: Upper-case symbols; at least one.
        start: Inclusive lower bound, naive UTC like the column.
        end: Exclusive upper bound, naive UTC.
        limit: Maximum rows to return; the caller bounds it.
    """
    result = await session.execute(
        _ARTICLES_IN_WINDOW_SQL,
        {"tickers": tickers, "start": start, "end": end, "limit": limit},
    )
    return [
        ArticleRow(
            id=int(row.id),
            ticker=str(row.ticker),
            article_url=row.article_url,
            published_at=row.published_at,
            sentiment_score=float(row.sentiment_score),
            content_summary=row.content_summary,
        )
        for row in result
    ]


async def article_by_id(session: AsyncSession, article_id: int) -> ArticleRow | None:
    """One scored article, or None when no such row exists."""
    row = (await session.execute(_ARTICLE_BY_ID_SQL, {"id": article_id})).first()
    if row is None:
        return None
    return ArticleRow(
        id=int(row.id),
        ticker=str(row.ticker),
        article_url=row.article_url,
        published_at=row.published_at,
        sentiment_score=float(row.sentiment_score),
        content_summary=row.content_summary,
    )


async def scores_between(
    session: AsyncSession, ticker: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """``(published_at, score)`` for ``ticker`` in the half-open range ``(start, end]``.

    ``start`` and ``end`` are naive UTC, matching the column.
    """
    result = await session.execute(
        _SCORES_IN_RANGE_SQL, {"ticker": ticker, "start": start, "end": end}
    )
    return [(row.published_at, float(row.sentiment_score)) for row in result]
