"""Load a gated run's article scores from the live news database.

Runs inside the worker, before the engine starts, so the engine receives plain
data and never touches a database. Reads go through a connection Postgres holds
read-only (``create_news_sync_engine``); ``public.news_sentiment`` is never
written.

Any failure here fails the run. A gated run that silently ran ungated would
report results for a strategy the student did not ask for.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
import logging

from sqlalchemy import Engine, bindparam, text

from engine.core.sentiment_gate import DATE_ONLY_EMBARGO, SENTIMENT_WINDOW, SentimentGate
from src.db.engine import create_news_sync_engine

logger = logging.getLogger(__name__)

# Articles that can influence a bar in [start, end]: the 7-day window before the
# first bar, widened by the longest embargo so a late-available article is kept.
_LOOKBACK = SENTIMENT_WINDOW + DATE_ONLY_EMBARGO
_LOOKAHEAD = DATE_ONLY_EMBARGO

# Per ticker, on the (ticker, published_at) index; only the two columns used.
_SCORES_SQL = text(
    "SELECT published_at, sentiment_score FROM public.news_sentiment "
    "WHERE ticker = :ticker AND sentiment_score IS NOT NULL "
    "AND published_at >= :start AND published_at <= :end"
).bindparams(bindparam("ticker"), bindparam("start"), bindparam("end"))


class SentimentGateUnavailable(RuntimeError):
    """The live news database could not supply a gated run's scores."""


def load_sentiment_gate(
    threshold: float,
    tickers: list[str],
    start_date: date,
    end_date: date,
    engine_factory: Callable[[], Engine] = create_news_sync_engine,
) -> SentimentGate:
    """Build the gate for one run from the live article scores.

    Args:
        threshold: The gate threshold, already validated to [-1, 0].
        tickers: The run's universe.
        start_date: First session of the run.
        end_date: Last session of the run.
        engine_factory: Where the read-only engine comes from; tests pass a fake.

    Raises:
        SentimentGateUnavailable: the database is unconfigured or unreachable.
    """
    window_start = datetime.combine(start_date, time.min) - _LOOKBACK
    window_end = datetime.combine(end_date, time.max) + _LOOKAHEAD
    wanted = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    try:
        engine = engine_factory()
    except RuntimeError as exc:
        raise SentimentGateUnavailable(f"Sentiment gate unavailable: {exc}") from exc
    try:
        articles = _read_scores(engine, wanted, window_start, window_end)
    except Exception as exc:
        # Driver messages can carry connection details; the type is enough to act on.
        raise SentimentGateUnavailable(
            "Sentiment gate unavailable: the live news database could not be read "
            f"({type(exc).__name__})."
        ) from exc
    finally:
        engine.dispose()
    logger.info(
        "SENTIMENT GATE | threshold=%s tickers=%d articles=%d window=%s..%s",
        threshold,
        len(wanted),
        sum(len(rows) for rows in articles.values()),
        window_start.date(),
        window_end.date(),
    )
    return SentimentGate(threshold=threshold, articles=articles)


def _read_scores(
    engine: Engine, tickers: list[str], start: datetime, end: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    articles: dict[str, list[tuple[datetime, float]]] = {}
    with engine.connect() as connection:
        try:
            for ticker in tickers:
                rows = connection.execute(
                    _SCORES_SQL, {"ticker": ticker, "start": start, "end": end}
                )
                articles[ticker] = [(row[0], float(row[1])) for row in rows]
        finally:
            # Nothing to keep: end the read-only transaction without a commit.
            connection.rollback()
    return articles
