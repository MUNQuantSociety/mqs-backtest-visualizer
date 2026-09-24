"""Read-only access to ``public.news_sentiment``.

The MQSMaster NLP pipeline owns this table: one row per article URL, scored by
the fine-tuned FinBERT model into ``sentiment_score`` in [-1, 1]. This
application only reads it, for the dashboard's news list and sentiment gauges,
and must never write to it.

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

# Newest first over the published_at index.
_LATEST_ARTICLES_SQL = text(
    _ARTICLE_COLUMNS + "ORDER BY published_at DESC, id DESC LIMIT :limit"
).bindparams(bindparam("limit"))

# Same, restricted to a ticker set; served by the (ticker, published_at) index.
_LATEST_ARTICLES_FOR_TICKERS_SQL = text(
    _ARTICLE_COLUMNS
    + "AND ticker IN :tickers ORDER BY published_at DESC, id DESC LIMIT :limit"
).bindparams(bindparam("tickers", expanding=True), bindparam("limit"))

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


async def latest_articles(
    session: AsyncSession, tickers: list[str] | None, limit: int
) -> list[ArticleRow]:
    """The ``limit`` most recently published articles, optionally for ``tickers`` only.

    Args:
        session: An open async session.
        tickers: Upper-case symbols to restrict to, or None for every ticker.
        limit: Maximum rows to return; the caller bounds it.
    """
    if tickers is None:
        result = await session.execute(_LATEST_ARTICLES_SQL, {"limit": limit})
    else:
        result = await session.execute(
            _LATEST_ARTICLES_FOR_TICKERS_SQL, {"tickers": tickers, "limit": limit}
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
