"""Market-data coverage: what the run form is allowed to offer.

FMP or the database supplies one span per ticker. The intersection is shared
by the run form, submission validation and uploaded-strategy validation.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from functools import lru_cache
import logging
import re
import threading
import time
from zoneinfo import ZoneInfo

from engine.data.fmp import FMPMarketData, market_data_source
from engine.data.fmp import FMPUnavailable as FMPUnavailable

from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import market_data as market_data_repo
from src.repositories import strategies as strategies_repo
from src.schemas.market_data import CoverageResponse, TickerCoverage, TickerValidation, TickerValidationResponse

logger = logging.getLogger(__name__)

# Only successful provider answers are cached. Coalesce concurrent checks for
# the same symbol; bound active provider requests across all API callers.
_symbol_cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()
_symbol_pending: dict[str, Future] = {}
_symbol_lock = threading.Lock()
_symbol_requests = threading.BoundedSemaphore(4)
_symbol_clock = time.monotonic


def normalize_tickers(tickers: list[str]) -> list[str]:
    if not 1 <= len(tickers) <= 50 or any(not isinstance(t, str) for t in tickers):
        raise ValueError("Pass between 1 and 50 ticker symbols.")
    wanted = [ticker.strip().upper() for ticker in tickers]
    if any(not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}", ticker) for ticker in wanted):
        raise ValueError("Enter valid ticker symbols of at most 20 characters.")
    return list(dict.fromkeys(wanted))


def _fmp_symbol_exists(ticker: str) -> bool:
    with _symbol_lock:
        cached = _symbol_cache.get(ticker)
        if cached is not None and cached[0] > _symbol_clock():
            _symbol_cache.move_to_end(ticker)
            return cached[1]
        pending = _symbol_pending.get(ticker)
        leader = pending is None
        if leader:
            pending = _symbol_pending[ticker] = Future()
    if not leader:
        return pending.result()
    try:
        with _symbol_requests:
            exists = FMPMarketData().symbol_exists(ticker)
        with _symbol_lock:
            _symbol_cache[ticker] = (_symbol_clock() + (3600 if exists else 300), exists)
            _symbol_cache.move_to_end(ticker)
            while len(_symbol_cache) > 512:
                _symbol_cache.popitem(last=False)
        pending.set_result(exists)
        return exists
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _symbol_lock:
            _symbol_pending.pop(ticker, None)


async def validate_tickers(tickers: list[str]) -> TickerValidationResponse:
    """Look up FMP metadata only; no database price or history request."""
    wanted = normalize_tickers(tickers)
    limit = asyncio.Semaphore(4)

    async def lookup(ticker):
        async with limit:
            exists = await asyncio.to_thread(_fmp_symbol_exists, ticker)
            return TickerValidation(ticker=ticker, status="valid" if exists else "unknown")

    tasks = [asyncio.create_task(lookup(ticker)) for ticker in wanted]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # Stop queued work when one provider check fails. In-flight sockets
        # retain the provider's short timeout and bounded transient retry.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return TickerValidationResponse(tickers=results, unknown=[r.ticker for r in results if r.status == "unknown"])


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


@lru_cache(maxsize=512)
def _fmp_span(ticker: str, as_of: date, cache_period: int) -> tuple[date, date] | None:
    # Cache successful answers briefly while the user edits the form. An
    # exception is never cached or interpreted as an absent ticker.
    rows = FMPMarketData().get_historical_data(ticker, date(1900, 1, 1), as_of)
    days = [row["date"] for row in rows]
    return (min(days), max(days)) if days else None


async def _fmp_coverage(tickers: list[str]) -> dict[str, tuple[date, date] | None]:
    # Only offer completed prior exchange dates, excluding today's live bar.
    as_of = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    cache_period = int(time.monotonic() // 300)
    limit = asyncio.Semaphore(4)

    async def lookup(ticker):
        async with limit:
            span = await asyncio.to_thread(_fmp_span, ticker, as_of, cache_period)
            return ticker, span

    return dict(await asyncio.gather(*(lookup(ticker) for ticker in tickers)))


async def coverage_for(tickers: list[str]) -> CoverageResponse:
    """Coverage for a ticker set, and the window safe for every one of them.

    This reads FMP or ``public.market_data``. Neither needs the app schema,
    so no schema initialization or database session is opened for FMP prices.
    """
    started = time.perf_counter()
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    source = market_data_source()
    logger.info("COVERAGE | Checking market-data bounds; source=%s tickers=%s", source, tickers)
    if source == "fmp":
        spans = await _fmp_coverage(tickers)
    else:
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
    have_window = bool(starts) and not missing and max(starts) <= min(ends)
    logger.info(
        "COVERAGE | tickers=%s start=%s end=%s missing=%s elapsed_ms=%.0f",
        tickers, _iso(max(starts)) if have_window else None,
        _iso(min(ends)) if have_window else None, missing,
        (time.perf_counter() - started) * 1000,
    )
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
