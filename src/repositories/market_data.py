"""Access to ``public.market_data``: reads, plus the one guarded write the backfill makes.

The live trading system owns this table. This application reads it and must
never write to it.

Use the existing (ticker, timestamp) index for both ends of each ticker's
history. Ordering by date instead scans the global date index and filters out
other tickers, which can take minutes for recently listed symbols. Return the
stored exchange date, not a timestamp converted in the client's timezone.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# Earliest/latest observations use the same ticker-specific index.
_LATEST_BAR_SQL = text(
    "SELECT date FROM public.market_data "
    'WHERE ticker = :ticker ORDER BY "timestamp" DESC LIMIT 1'
).bindparams(bindparam("ticker"))

# The other end of the same index, read the same way and for the same reason.
_EARLIEST_BAR_SQL = text(
    "SELECT date FROM public.market_data "
    'WHERE ticker = :ticker ORDER BY "timestamp" ASC LIMIT 1'
).bindparams(bindparam("ticker"))


async def latest_market_data_date(
    session: AsyncSession, tickers: list[str]
) -> date | None:
    """The last day every one of ``tickers`` has a bar for, or None.

    The earliest of the per-ticker maxima, because a window running past one
    ticker's coverage is a window the engine has no prices for.

    Callers anchor a validation window on this rather than on today's date.
    Market data ends weeks behind the calendar, so a window computed from
    ``now()`` returns no rows and fails every upload.
    """
    wanted = [str(ticker).strip() for ticker in tickers if str(ticker).strip()]
    if not wanted:
        return None

    latest: date | None = None
    for ticker in wanted:
        row = (await session.execute(_LATEST_BAR_SQL, {"ticker": ticker})).first()
        if row is None or row[0] is None:
            # A ticker with no bars at all: there is no window that covers the
            # universe, and saying so beats running against a partial one.
            return None
        latest = row[0] if latest is None else min(latest, row[0])
    return latest


async def ticker_coverage(
    session: AsyncSession, tickers: list[str]
) -> dict[str, tuple[date, date] | None]:
    """First and last bar per ticker, or None for a ticker with no bars.

    Used by ``GET /market-data/coverage`` to bound the run form's date picker.
    """
    wanted = [str(ticker).strip() for ticker in tickers if str(ticker).strip()]

    coverage: dict[str, tuple[date, date] | None] = {}
    for ticker in wanted:
        if ticker in coverage:
            continue
        first = (await session.execute(_EARLIEST_BAR_SQL, {"ticker": ticker})).first()
        last = (await session.execute(_LATEST_BAR_SQL, {"ticker": ticker})).first()
        if first is None or last is None or first[0] is None or last[0] is None:
            coverage[ticker] = None
            continue
        coverage[ticker] = (first[0], last[0])
    return coverage


# A "loose index scan": walk the (ticker, timestamp) index one ticker at a
# time instead of reading every bar for a DISTINCT. On the live table that is
# the difference between touching a few hundred index entries and scanning
# years of intraday rows every five minutes.
_DISTINCT_TICKERS_SQL = text(
    "WITH RECURSIVE walk AS ("
    "  (SELECT ticker FROM public.market_data ORDER BY ticker LIMIT 1)"
    "  UNION ALL"
    "  SELECT (SELECT ticker FROM public.market_data WHERE ticker > walk.ticker"
    "          ORDER BY ticker LIMIT 1)"
    "  FROM walk WHERE walk.ticker IS NOT NULL"
    ") SELECT ticker FROM walk WHERE ticker IS NOT NULL"
)


async def loaded_tickers(session: AsyncSession) -> set[str]:
    """Every ticker with at least one bar: what this database can already run."""
    result = await session.execute(_DISTINCT_TICKERS_SQL)
    return {str(row[0]).strip().upper() for row in result if row[0]}


# Every wanted ticker's newest bar in one round trip. The lateral LIMIT 1 walks
# each ticker's end of the (ticker, timestamp) index, the same plan as
# _LATEST_BAR_SQL; a DISTINCT ON (ticker) here would read every bar instead.
_LATEST_BARS_SQL = text(
    "SELECT wanted.ticker, latest.date "
    "FROM unnest(CAST(:tickers AS text[])) AS wanted(ticker) "
    "CROSS JOIN LATERAL ("
    "  SELECT date FROM public.market_data "
    '  WHERE ticker = wanted.ticker ORDER BY "timestamp" DESC LIMIT 1'
    ") AS latest"
).bindparams(bindparam("tickers"))

# One close per New York session: the last bar inside 09:30-16:00, the same rule
# the engine's historical loader uses, so dashboard indicators and backtests see
# the same closes. Each ticker carries its own lower bound, and the join on
# ticker and timestamp keeps every ticker's read on the (ticker, timestamp) index.
_DAILY_CLOSES_SQL = text(
    "SELECT DISTINCT ON (bar.ticker, (bar.\"timestamp\" AT TIME ZONE 'America/New_York')::date) "
    "bar.ticker, (bar.\"timestamp\" AT TIME ZONE 'America/New_York')::date AS trade_date, "
    "bar.close_price "
    "FROM unnest(CAST(:tickers AS text[]), CAST(:sinces AS timestamptz[])) "
    "AS wanted(ticker, since) "
    "JOIN public.market_data AS bar "
    "ON bar.ticker = wanted.ticker AND bar.\"timestamp\" >= wanted.since "
    "WHERE bar.close_price IS NOT NULL "
    "AND (bar.\"timestamp\" AT TIME ZONE 'America/New_York')::time BETWEEN '09:30' AND '16:00' "
    "ORDER BY bar.ticker, (bar.\"timestamp\" AT TIME ZONE 'America/New_York')::date, "
    "bar.\"timestamp\" DESC"
).bindparams(bindparam("tickers"), bindparam("sinces"))

# Transaction-local, so the limit ends with the caller's transaction and never
# leaks onto a pooled connection.
_STATEMENT_TIMEOUT_SQL = text(
    "SELECT set_config('statement_timeout', :milliseconds, true)"
).bindparams(bindparam("milliseconds"))


async def limit_statement_time(session: AsyncSession, milliseconds: int) -> None:
    """Cancel any later statement in this transaction that runs past ``milliseconds``."""
    await session.execute(_STATEMENT_TIMEOUT_SQL, {"milliseconds": str(milliseconds)})


async def last_bar_dates(session: AsyncSession, tickers: list[str]) -> dict[str, date]:
    """The stored exchange date of each ticker's newest bar; tickers with no bars are absent.

    Args:
        session: An open async session.
        tickers: Exact stored symbols.
    """
    if not tickers:
        return {}
    result = await session.execute(_LATEST_BARS_SQL, {"tickers": tickers})
    return {row.ticker: row.date for row in result if row.date is not None}


async def daily_closes(
    session: AsyncSession, since_by_ticker: dict[str, datetime]
) -> dict[str, list[tuple[date, float]]]:
    """Session closes per ticker from its own lower bound onward, oldest first.

    Tickers with no closes in range are absent from the result.

    Args:
        session: An open async session.
        since_by_ticker: Exact stored symbol to its timezone-aware lower bound
            on the bar timestamp.
    """
    if not since_by_ticker:
        return {}
    result = await session.execute(
        _DAILY_CLOSES_SQL,
        {"tickers": list(since_by_ticker), "sinces": list(since_by_ticker.values())},
    )
    closes: dict[str, list[tuple[date, float]]] = {}
    for row in result:
        closes.setdefault(row.ticker, []).append((row.trade_date, float(row.close_price)))
    return closes


_INSERT_BARS_SQL = text(
    "INSERT INTO public.market_data "
    "(ticker, timestamp, date, exchange, open_price, high_price, low_price, close_price, volume) "
    "VALUES (:ticker, :timestamp, :date, :exchange, :open_price, :high_price, :low_price, :close_price, :volume) "
    "ON CONFLICT (ticker, timestamp) DO NOTHING"
)


_COUNT_BARS_SQL = text(
    "SELECT count(*) FROM public.market_data "
    "WHERE ticker = :ticker AND timestamp BETWEEN :first AND :last"
)


async def insert_daily_bars(session: AsyncSession, rows: list[dict]) -> int:
    """Add bars the table does not have; the ones it has are left exactly as they were.

    The unique (ticker, timestamp) index makes this idempotent, and it is the
    reason a backfill never overwrites: the live system owns its own rows.
    Returns how many were actually written — counted, because a batched
    insert's rowcount is -1 on asyncpg and says nothing.
    """
    if not rows:
        return 0
    span = {
        "ticker": rows[0]["ticker"],
        "first": min(row["timestamp"] for row in rows),
        "last": max(row["timestamp"] for row in rows),
    }
    before = await session.scalar(_COUNT_BARS_SQL, span)
    await session.execute(_INSERT_BARS_SQL, rows)
    after = await session.scalar(_COUNT_BARS_SQL, span)
    return int(after or 0) - int(before or 0)
