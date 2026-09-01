"""Read-only access to ``public.market_data``.

The live trading system owns this table. This application reads it and must
never write to it.

Both queries here are per ticker rather than aggregates over a ticker set. That
is measured, not stylistic: the planner answers the per-ticker form from the
descending date index in under a millisecond, while the aggregate form walks
the index and takes minutes on a table this size.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# One ticker's most recent bar. See the module docstring for why this is not
# ``max(date)`` over a ticker set.
_LATEST_BAR_SQL = text(
    "SELECT date FROM public.market_data "
    "WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"
).bindparams(bindparam("ticker"))

# The other end of the same index, read the same way and for the same reason.
_EARLIEST_BAR_SQL = text(
    "SELECT date FROM public.market_data "
    "WHERE ticker = :ticker ORDER BY date ASC LIMIT 1"
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
