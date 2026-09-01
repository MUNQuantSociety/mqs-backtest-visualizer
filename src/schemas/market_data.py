"""Market-data coverage models.

Mirrors ``src/features/market-data/types.ts``. One endpoint, one question:
which dates does this application actually have prices for?

The question matters because the answer is not "up to today". Coverage ends
weeks behind the calendar, so a run form that offers "last 30 days" produces a
window with no rows in it and a run that fails for a reason the student did not
cause.
"""

from __future__ import annotations

from src.schemas.common import CamelModel


class TickerCoverage(CamelModel):
    """The span of bars held for one ticker.

    ``firstBar`` and ``lastBar`` are null together, and only when the ticker has
    no bars at all. That is reported rather than omitted: a universe naming a
    ticker nothing has data for cannot be backtested, and saying which one is
    the whole diagnosis.
    """

    ticker: str
    first_bar: str | None = None
    last_bar: str | None = None


class CoverageResponse(CamelModel):
    """Per-ticker spans, plus the window that is safe for all of them.

    ``start`` is the latest of the first bars and ``end`` the earliest of the
    last bars, which is the intersection: a window running past one ticker's
    coverage is a window the engine has no prices for. Both are null when any
    ticker is missing, because there is then no window that covers the universe.
    """

    tickers: list[TickerCoverage]
    start: str | None = None
    end: str | None = None
    # Tickers with no bars at all. Empty is the normal case.
    missing: list[str] = []
