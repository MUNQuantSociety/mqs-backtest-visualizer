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

from sqlalchemy.exc import SQLAlchemyError

from engine.data import yahoo
from engine.data.fmp import FMPMarketData, FMPSymbolUnknown, fetch_daily_history, market_data_source
from engine.data.fmp import FMPUnavailable as FMPUnavailable

from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import market_data as market_data_repo
from src.repositories import reports as reports_repo
from src.repositories import strategies as strategies_repo
from src.schemas.market_data import (
    ClosePoint,
    TickerClosesResponse,
    CoverageResponse,
    SymbolMatch,
    SymbolSearchResponse,
    TickerCoverage,
    TickerValidation,
    TickerValidationResponse,
)

logger = logging.getLogger(__name__)

# Only successful provider answers are cached. Coalesce concurrent checks for
# the same symbol; bound active provider requests across all API callers.
_symbol_cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()
_symbol_pending: dict[str, Future] = {}
_symbol_lock = threading.Lock()
_symbol_requests = threading.BoundedSemaphore(4)
_symbol_clock = time.monotonic

# Prefix suggestions, keyed by the normalized prefix. Same lock and the same
# semaphore as the exact lookups: the provider cap is one number for the
# whole process, not one per endpoint. A short TTL because a prefix page is
# only ever a hint; the exact lookup remains the verdict.
SYMBOL_SEARCH_RESULTS = 10
_SEARCH_TTL_SECONDS = 300
_SEARCH_CACHE_SIZE = 256
_search_cache: OrderedDict[str, tuple[float, list[SymbolMatch], bool]] = OrderedDict()
_search_pending: dict[str, Future] = {}
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}")

# The engine trades the New York session; a listing elsewhere would validate
# and then run against the wrong calendar. Spelled the way each provider does.
_US_EXCHANGES_FMP = frozenset({"NYSE", "NASDAQ", "AMEX"})
_US_EXCHANGES_YAHOO = frozenset({"NYSE", "NASDAQ", "NYSEARCA", "NYSE AMERICAN", "AMEX", "BATS"})

# Tickers this deployment already knows: every symbol with bars in
# public.market_data and every symbol a saved report has traded. Answered from
# memory, so a suggestion for them costs nothing and validation skips the
# provider. Re-read every few minutes and whenever a report is saved.
_KNOWN_TTL_SECONDS = 300
_KNOWN_RETRY_SECONDS = 30
_known_tickers: tuple[float, dict[str, str]] | None = None
_known_lock = threading.Lock()


# A benchmark window as long as the dashboard's "max" period needs, and no more:
# each extra year is ~252 more index probes.
_CLOSES_MAX_SPAN = timedelta(days=366 * 15)
# The same budget GET /indicators gives its closes query.
_CLOSES_STATEMENT_TIMEOUT_MS = 15_000
_EXCHANGE_TZ = ZoneInfo("America/New_York")


class MarketDataUnavailable(RuntimeError):
    """The market-data database could not answer; the route turns this into a 503."""


async def closes_between(ticker: str, start: date, end: date) -> TickerClosesResponse:
    """A ticker's daily session closes from ``start`` to ``end`` (inclusive), oldest first.

    Args:
        ticker: One symbol, case-insensitive.
        start: First New York trading date to include.
        end: Last New York trading date to include.

    Returns:
        The closes found; a ticker with none in range has no points.

    Raises:
        ValueError: an invalid ticker, ``start`` after ``end``, or a window longer
            than 15 years.
        MarketDataUnavailable: the database failed or ran past its time budget.
    """
    (wanted,) = normalize_tickers([ticker])
    if start > end:
        raise ValueError("start must be on or before end.")
    if end - start > _CLOSES_MAX_SPAN:
        raise ValueError("Ask for at most 15 years of closes at a time.")
    since = datetime.combine(start, datetime.min.time(), tzinfo=_EXCHANGE_TZ)
    try:
        async with session_scope() as session:
            await market_data_repo.limit_statement_time(session, _CLOSES_STATEMENT_TIMEOUT_MS)
            closes = await market_data_repo.daily_closes(session, {wanted: (since, end)})
    except (SQLAlchemyError, OSError) as exc:
        # The driver's message can carry connection details; the type is enough.
        logger.error("Closes query failed for %s: %s", wanted, type(exc).__name__)
        raise MarketDataUnavailable("Benchmark prices are unavailable. Please try again later.") from exc
    return TickerClosesResponse(
        ticker=wanted,
        points=[
            ClosePoint(date=day.isoformat(), close=close)
            for day, close in closes.get(wanted, [])
        ],
    )


def normalize_tickers(tickers: list[str]) -> list[str]:
    if not 1 <= len(tickers) <= 50 or any(not isinstance(t, str) for t in tickers):
        raise ValueError("Pass between 1 and 50 ticker symbols.")
    wanted = [ticker.strip().upper() for ticker in tickers]
    if any(not _SYMBOL_PATTERN.fullmatch(ticker) for ticker in wanted):
        raise ValueError("Enter valid ticker symbols of at most 20 characters.")
    return list(dict.fromkeys(wanted))


def normalize_search_query(query: str) -> str:
    """The prefix a suggestion request may ask about: a partial ticker, nothing more."""
    prefix = query.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(prefix):
        raise ValueError("Enter part of a ticker symbol, at most 20 characters.")
    return prefix


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


def _remember_symbols(symbols: list[str]) -> None:
    """A symbol the provider just listed exists; say so before anyone asks.

    Called under ``_symbol_lock``. The selection that follows a suggestion
    goes through the exact lookup, and this is what makes that lookup a
    cache hit rather than a second provider call for the same page. Only a
    positive verdict is written: a page can prove presence, never absence.
    A symbol someone is already looking up is left to that lookup.
    """
    expires = _symbol_clock() + 3600
    for symbol in symbols:
        if symbol in _symbol_pending:
            continue
        _symbol_cache[symbol] = (expires, True)
        _symbol_cache.move_to_end(symbol)
    while len(_symbol_cache) > 512:
        _symbol_cache.popitem(last=False)


def _to_match(row: dict, source: str) -> SymbolMatch:
    name = row.get("name")
    exchange = row.get("exchangeFullName") or row.get("exchange")
    return SymbolMatch(
        symbol=row["symbol"].strip().upper(),
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        exchange=exchange.strip() if isinstance(exchange, str) and exchange.strip() else None,
        source=source,
    )


def _is_us_listing(row: dict) -> bool:
    code = row.get("exchange")
    currency = row.get("currency")
    return (
        isinstance(code, str) and code.strip().upper() in _US_EXCHANGES_FMP
        and (currency is None or (isinstance(currency, str) and currency.upper() == "USD"))
    )


def _fmp_search_symbols(prefix: str) -> tuple[list[SymbolMatch], bool]:
    """FMP's symbols starting with ``prefix`` and companies named like it, US listings only."""
    with _symbol_lock:
        cached = _search_cache.get(prefix)
        if cached is not None and cached[0] > _symbol_clock():
            _search_cache.move_to_end(prefix)
            return cached[1], cached[2]
        pending = _search_pending.get(prefix)
        leader = pending is None
        if leader:
            pending = _search_pending[prefix] = Future()
    if not leader:
        return pending.result()
    try:
        with _symbol_requests:
            provider = FMPMarketData()
            # One more than we show, so a full page is known to be a cut.
            by_symbol = provider.search_symbols(prefix, limit=SYMBOL_SEARCH_RESULTS + 1)
            # Two letters match too many company names to be worth a call;
            # by then the symbol search is the better guess anyway.
            by_name = provider.search_names(prefix, limit=SYMBOL_SEARCH_RESULTS + 1) if len(prefix) >= 3 else []
        seen: set[str] = set()
        rows: list[dict] = []
        for row in [*by_symbol, *by_name]:
            symbol = row["symbol"].strip().upper()
            if symbol in seen or not _is_us_listing(row):
                continue
            seen.add(symbol)
            rows.append(row)
        truncated = len(rows) > SYMBOL_SEARCH_RESULTS
        matches = [_to_match(row, "fmp") for row in rows[:SYMBOL_SEARCH_RESULTS]]
        with _symbol_lock:
            _search_cache[prefix] = (_symbol_clock() + _SEARCH_TTL_SECONDS, matches, truncated)
            _search_cache.move_to_end(prefix)
            while len(_search_cache) > _SEARCH_CACHE_SIZE:
                _search_cache.popitem(last=False)
            _remember_symbols([match.symbol for match in matches])
        pending.set_result((matches, truncated))
        return matches, truncated
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _symbol_lock:
            _search_pending.pop(prefix, None)


def _yahoo_search_symbols(prefix: str) -> tuple[list[SymbolMatch], bool]:
    """The unofficial fallback, US equities and ETFs only. Never warms the exact cache."""
    with _symbol_requests:
        quotes = yahoo.search_symbols(prefix)
    rows: list[dict] = []
    seen: set[str] = set()
    for quote in quotes:
        symbol = quote.get("symbol")
        exchange = quote.get("exchDisp") or quote.get("exchange")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        if quote.get("quoteType") not in {"EQUITY", "ETF"}:
            continue
        if not isinstance(exchange, str) or exchange.strip().upper() not in _US_EXCHANGES_YAHOO:
            continue
        symbol = symbol.strip().upper()
        if symbol in seen or not symbol.startswith(prefix):
            continue
        seen.add(symbol)
        rows.append({"symbol": symbol, "name": quote.get("shortname") or quote.get("longname"), "exchange": exchange})
    truncated = len(rows) > SYMBOL_SEARCH_RESULTS
    return [_to_match(row, "yahoo") for row in rows[:SYMBOL_SEARCH_RESULTS]], truncated


async def load_known_tickers() -> dict[str, str]:
    """Ticker → why it is known. Reads the database; replaced in tests.

    Database mode only. With FMP as the price source the provider is the
    authority on what exists, and coverage in that mode is promised never to
    open a database session — a promise the index would otherwise break.
    """
    if market_data_source() == "fmp":
        return {}
    known: dict[str, str] = {}
    async with session_scope() as session:
        try:
            for ticker in await market_data_repo.loaded_tickers(session):
                known[ticker] = "database"
        except Exception:  # noqa: BLE001 - the live table may not exist on this database
            logger.warning("SEARCH | public.market_data is not readable; known tickers come from runs only")
        for ticker in await reports_repo.run_tickers(session):
            known.setdefault(ticker, "run")
    return known


async def known_tickers() -> dict[str, str]:
    global _known_tickers
    now = _symbol_clock()
    with _known_lock:
        if _known_tickers is not None and _known_tickers[0] > now:
            return _known_tickers[1]
    try:
        loaded = await load_known_tickers()
        ttl = _KNOWN_TTL_SECONDS
    except Exception:  # noqa: BLE001 - suggestions must not fail because the index could not load
        logger.exception("SEARCH | Known tickers could not be loaded; retrying shortly")
        loaded, ttl = {}, _KNOWN_RETRY_SECONDS
    with _known_lock:
        _known_tickers = (_symbol_clock() + ttl, loaded)
    return loaded


def invalidate_known_tickers() -> None:
    """A report was saved or bars were written: the next request re-reads."""
    global _known_tickers
    with _known_lock:
        _known_tickers = None


async def search_symbols(query: str) -> SymbolSearchResponse:
    """Suggestions for ``query``: what this deployment knows, then what the providers know.

    Known tickers come first and cost nothing. The provider is asked only
    when they leave room on the page. FMP is the provider; Yahoo's unofficial
    search stands in when FMP cannot answer, and when neither can, the known
    tickers are still returned with the failure noted rather than a 503 —
    unless there is nothing at all to show.
    """
    prefix = normalize_search_query(query)
    known = await known_tickers()
    known_matches = [
        SymbolMatch(symbol=ticker, source=source)
        for ticker, source in sorted(known.items())
        if ticker.startswith(prefix)
    ]
    if len(known_matches) >= SYMBOL_SEARCH_RESULTS:
        return SymbolSearchResponse(matches=known_matches[:SYMBOL_SEARCH_RESULTS], truncated=True)

    provider_error: str | None = None
    provider_matches: list[SymbolMatch] = []
    truncated = False
    try:
        provider_matches, truncated = await asyncio.to_thread(_fmp_search_symbols, prefix)
    except FMPUnavailable as fmp_error:
        if settings.symbol_search_yahoo_fallback:
            try:
                provider_matches, truncated = await asyncio.to_thread(_yahoo_search_symbols, prefix)
            except yahoo.YahooUnavailable as yahoo_error:
                logger.warning("SEARCH | Both providers failed; fmp=%s yahoo=%s", fmp_error, yahoo_error)
                provider_error = str(fmp_error)
        else:
            provider_error = str(fmp_error)
        if provider_error is not None and not known_matches:
            raise fmp_error

    known_symbols = {match.symbol for match in known_matches}
    merged = known_matches + [m for m in provider_matches if m.symbol not in known_symbols]
    if len(merged) > SYMBOL_SEARCH_RESULTS:
        truncated = True
    return SymbolSearchResponse(
        matches=merged[:SYMBOL_SEARCH_RESULTS], truncated=truncated, provider_error=provider_error
    )


async def validate_tickers(tickers: list[str]) -> TickerValidationResponse:
    """Look up FMP metadata only; no database price or history request."""
    wanted = normalize_tickers(tickers)
    known = await known_tickers()
    limit = asyncio.Semaphore(4)

    async def lookup(ticker):
        # Bars in the database or a saved run: the symbol is real, no need
        # to spend a provider call proving it.
        if ticker in known:
            return TickerValidation(ticker=ticker, status="valid")
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
    # Cache successful answers briefly while the user edits the form. A
    # provider failure is never cached or interpreted as an absent ticker —
    # except FMP's own "no such symbol" (404), which is the same fact as an
    # empty history and the reason the validate-tickers endpoint exists.
    try:
        rows = FMPMarketData().get_historical_data(ticker, date(1900, 1, 1), as_of)
    except FMPSymbolUnknown:
        return None
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


async def backfill_missing(tickers: list[str], start: date, end: date) -> dict[str, int]:
    """Load daily bars for ``tickers`` into ``public.market_data`` over ``start..end``.

    The port of MQSMaster's ``specific_backfill``: fetch from FMP, insert what
    the table lacks, leave what it has. Daily 16:00 New York bars rather than
    MQSMaster's intraday minutes, because that is what a backtest here reads
    and it is one provider call per ticker for the whole window. A ticker the
    provider has nothing for is simply reported with 0 — the caller's
    coverage still shows it missing, and that is the honest answer.
    """
    written: dict[str, int] = {}
    for ticker in tickers:
        try:
            frame = await asyncio.to_thread(fetch_daily_history, [ticker], start, end, require_all=False)
            exchange = await asyncio.to_thread(_listing_exchange, ticker)
        except FMPUnavailable as exc:
            logger.warning("BACKFILL | %s skipped; provider unavailable: %s", ticker, exc)
            written[ticker] = 0
            continue
        rows = [
            {
                "ticker": ticker,
                "timestamp": row.timestamp.to_pydatetime(),
                "date": row.timestamp.date(),
                "exchange": exchange,
                "open_price": float(row.open_price),
                "high_price": float(row.high_price),
                "low_price": float(row.low_price),
                "close_price": float(row.close_price),
                "volume": int(row.volume),
            }
            for row in frame.itertuples(index=False)
        ]
        async with session_scope() as session:
            written[ticker] = await market_data_repo.insert_daily_bars(session, rows)
        logger.info("BACKFILL | %s: %d bars written for %s..%s", ticker, written[ticker], start, end)
    if any(written.values()):
        invalidate_known_tickers()
    return written


# What the dev seed writes and the only value the table held before the
# backfill existed. Used when the provider does not name the listing.
_DEFAULT_EXCHANGE = "NASDAQ"


def _listing_exchange(ticker: str) -> str:
    """The exchange code FMP lists ``ticker`` on — NYSE, NASDAQ, AMEX.

    The daily history endpoint does not say where a bar traded, and inventing
    a venue would be wrong for two exchanges out of three. One symbol lookup
    answers it; the table's column is NOT NULL, so a symbol the provider does
    not place falls back to the seed's convention.
    """
    with _symbol_requests:
        rows = FMPMarketData().search_symbols(ticker)
    for row in rows:
        if row["symbol"].strip().upper() == ticker:
            code = row.get("exchange")
            if isinstance(code, str) and code.strip():
                return code.strip().upper()
    return _DEFAULT_EXCHANGE


def _backfill_window(spans: dict[str, tuple[date, date] | None]) -> tuple[date, date]:
    """The dates a new ticker should cover: what the others already do, else the last two years."""
    present = [span for span in spans.values() if span is not None]
    yesterday = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    if present:
        return min(first for first, _ in present), max(last for _, last in present)
    return yesterday - timedelta(days=730), yesterday


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
        # A ticker the form was just given and the table has never seen:
        # fetch its history now, over the window the rest of the universe
        # covers, and read the table again. The person sees the dot turn
        # green rather than a run they cannot start.
        absent = [ticker for ticker, span in spans.items() if span is None]
        if absent and settings.market_data_backfill_enabled:
            first, last = _backfill_window(spans)
            written = await backfill_missing(absent, first, last)
            if any(written.values()):
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
