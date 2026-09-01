"""Market-data coverage: what the run form is allowed to offer.

Thin by design. The repository answers one question per ticker and this module
turns the answers into the intersection the client needs, because "which window
can I run?" is a question about the universe, not about any single ticker.
"""

from __future__ import annotations

from datetime import date

from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import market_data as market_data_repo
from src.repositories import strategies as strategies_repo
from src.schemas.market_data import CoverageResponse, TickerCoverage


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


async def coverage_for(tickers: list[str]) -> CoverageResponse:
    """Coverage for a ticker set, and the window safe for every one of them.

    No ``ensure_schema()`` call: this reads ``public.market_data``, which the
    live trading system owns and this application only ever reads. Creating the
    ``app`` schema is not this endpoint's business.
    """
    async with session_scope() as session:
        spans = await market_data_repo.ticker_coverage(session, tickers)

    items: list[TickerCoverage] = []
    missing: list[str] = []
    starts: list[date] = []
    ends: list[date] = []

    for ticker, span in spans.items():
        if span is None:
            missing.append(ticker)
            items.append(TickerCoverage(ticker=ticker))
            continue
        first, last = span
        starts.append(first)
        ends.append(last)
        items.append(
            TickerCoverage(ticker=ticker, first_bar=_iso(first), last_bar=_iso(last))
        )

    # The intersection, and only when every ticker contributes one. A window
    # computed from a partial universe would look valid and run against
    # missing prices.
    have_window = bool(starts) and not missing
    return CoverageResponse(
        tickers=items,
        start=_iso(max(starts)) if have_window else None,
        end=_iso(min(ends)) if have_window else None,
        missing=missing,
    )


class UnknownStrategyError(LookupError):
    """The strategy key in the query string is not in the registry."""


async def universe_for_strategy(key: str) -> list[str]:
    """The ticker set a strategy trades, for coverage on that strategy alone.

    This one *does* touch the ``app`` schema, so it ensures it. The run form
    asks by strategy key rather than by ticker list because the student picks a
    strategy, not a universe, and the two must not be allowed to disagree.
    """
    await ensure_schema()
    async with session_scope() as session:
        strategy = await strategies_repo.get_strategy(session, key)
        if strategy is None:
            raise UnknownStrategyError(key)
        return list(strategy.universe or [])
