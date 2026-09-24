"""Intraday bars (1 minute to 1 hour) for event-mode backtests.

VISUALIZER: the vendored engine only ever loaded daily bars. This module adds
a second load path, chosen by the portfolio's ``BAR_INTERVAL_SECONDS`` config
key, that returns the same engine columns at a finer resolution.

Every bar is labelled at the moment its close is known — the end of its bucket,
capped at the 16:00 close — never at the bucket start. A start-labelled bar
would hand the strategy a close price up to an hour before it happened. The
daily path labels at 16:00 for the same reason.

Two sources, matching ``MARKET_DATA_SOURCE``:

* ``database`` buckets ``public.market_data`` rows in SQL. The table's own
  spacing is measured first, because a bucket finer than the stored bars
  cannot be built honestly (the store behind the default ``.env`` is hourly).
* ``fmp`` calls FMP's native intraday endpoints in chunks, because each
  request is silently truncated to its most recent few days.

Intraday rows never touch the parquet cache: that cache is keyed by ticker
alone and holds daily rows, so mixing resolutions would corrupt later runs.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from engine.contracts.errors import EngineError, MarketDataUnavailable, NoMarketData
from engine.data.fmp import FMPDataAdapter, FMPMarketData, FMPUnavailable, market_data_source

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

_FMP_INTERVAL_LABELS: dict[int, str] = {1: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "1hour"}
# Calendar days per FMP request, kept under the caps measured on 2026-09-23:
# a request returns only its latest ~3, ~10, ~45, ~30 and ~90 calendar days
# for 1min, 5min, 15min, 30min and 1hour respectively, and says nothing.
_FMP_CHUNK_DAYS: dict[int, int] = {1: 3, 5: 7, 15: 30, 30: 21, 60: 60}
_FMP_WORKERS = 4

_DB_TICKER_CHUNK = 10
_DB_SLAB_DAYS = 30
# The stored spacing is sampled in short windows this far apart, so a store
# that changes resolution part-way (finer recent rows, coarser old ones) is
# caught wherever the window reaches.
_SPACING_SAMPLE_DAYS = 5
_SPACING_SAMPLE_EVERY_DAYS = 60
# A ticker's first bar may trail the window start by a weekend plus a Monday
# holiday; later than this, the run would silently start late.
_LATE_START_TOLERANCE_DAYS = 4

_SESSION_FILTER = """
           AND (timestamp AT TIME ZONE 'America/New_York')::time >= '09:30'
           AND (timestamp AT TIME ZONE 'America/New_York')::time < '16:00'
"""

# The most common gap between consecutive in-session rows of the same ticker
# and day, over a short window: the resolution the table actually stores.
_SPACING_SQL = """
    SELECT gap_minutes, COUNT(*) AS occurrences
      FROM (
        SELECT EXTRACT(EPOCH FROM timestamp - LAG(timestamp) OVER (
                   PARTITION BY ticker,
                                (timestamp AT TIME ZONE 'America/New_York')::date
                   ORDER BY timestamp)) / 60 AS gap_minutes
          FROM market_data
         WHERE ticker IN ({placeholders})
           AND timestamp >= %s AND timestamp < %s
           {session_filter}
      ) gaps
     WHERE gap_minutes > 0
     GROUP BY gap_minutes
     ORDER BY occurrences DESC, gap_minutes ASC
     LIMIT 1
"""

# Buckets are aligned to 09:30 New York time. The width is a bound parameter;
# nothing caller-supplied is formatted into the text.
_BUCKET_SQL = """
    WITH session_rows AS (
        SELECT ticker, timestamp, open_price, high_price, low_price, close_price, volume,
               FLOOR(EXTRACT(EPOCH FROM (timestamp AT TIME ZONE 'America/New_York')::time
                                        - TIME '09:30') / 60 / %s) AS bucket_index,
               (timestamp AT TIME ZONE 'America/New_York')::date AS trade_date
          FROM market_data
         WHERE ticker IN ({placeholders})
           AND timestamp >= %s AND timestamp < %s
           {session_filter}
    )
    SELECT ticker,
           trade_date,
           bucket_index,
           (ARRAY_AGG(open_price ORDER BY timestamp ASC))[1] AS open_price,
           MAX(high_price) AS high_price,
           MIN(low_price) AS low_price,
           (ARRAY_AGG(close_price ORDER BY timestamp DESC))[1] AS close_price,
           SUM(volume) AS volume
      FROM session_rows
     GROUP BY ticker, trade_date, bucket_index
"""

_PRICE_COLUMNS = ("open_price", "high_price", "low_price", "close_price")


class IntradayResolutionUnavailable(EngineError):
    """The data source stores bars coarser than the requested bar size."""


class IntradayHistoryStartsLate(EngineError):
    """A ticker's intraday bars begin well after the simulated window starts.

    Intraday history is shallower than daily history (FMP's depth varies by
    symbol and interval; the database is pruned), and the window was checked
    against daily coverage. Without this, the run would quietly begin trading
    that ticker weeks late.
    """

    def __init__(self, late: dict[str, date | None], minutes: int) -> None:
        self.late = late
        described = ", ".join(
            f"{ticker} (first {minutes}-minute bar {day})" if day else f"{ticker} (no bars)"
            for ticker, day in late.items()
        )
        latest = max((day for day in late.values() if day), default=None)
        advice = f"Start the window on or after {latest}, " if latest else "Start later, "
        super().__init__(
            f"{minutes}-minute history begins after the window starts for {described}. "
            f"{advice}choose a 1-day timestep, or remove the ticker."
        )


def label_bar_close(bar_start: pd.Series, minutes: int) -> pd.Series:
    """The time each bar's close is known: its start plus its length, capped at 16:00.

    Args:
        bar_start: Timezone-aware New York bar start times.
        minutes: Bar length.
    """
    bar_end = bar_start + pd.Timedelta(minutes=minutes)
    session_close = bar_start.dt.normalize() + pd.Timedelta(hours=16)
    return bar_end.where(bar_end <= session_close, session_close)


def _session_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Midnight New York on ``start`` and on the day after ``end``: a half-open window."""
    lower = datetime.combine(start, time.min, tzinfo=NY_TZ)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=NY_TZ)
    return lower, upper


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(NY_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _finalize(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Engine columns, close-labelled, in-session, deduplicated and sorted."""
    if frame.empty:
        return pd.DataFrame()
    starts = frame["bar_start"]
    in_session = (starts.dt.time >= SESSION_OPEN) & (starts.dt.time < SESSION_CLOSE)
    frame = frame[in_session].copy()
    frame["timestamp"] = label_bar_close(frame.pop("bar_start"), minutes)
    for column in (*_PRICE_COLUMNS, "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "ticker", "close_price"])
    return (
        frame.drop_duplicates(["ticker", "timestamp"], keep="last")
        .sort_values(["timestamp", "ticker"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Database source
# ---------------------------------------------------------------------------


def _run_query(db: Any, sql: str, params: list[Any], label: str) -> list[dict]:
    """Execute through the engine adapter; a failed query is an outage, not an empty window."""
    try:
        result = db.execute_query(sql, params, fetch=True)
    except Exception as exc:  # noqa: BLE001 - adapters raise driver-specific types
        logger.exception("DB intraday query %s failed", label)
        raise MarketDataUnavailable(label, f"{type(exc).__name__}: {exc}") from exc
    if result.get("status") != "success":
        reason = str(result.get("message") or "<no message>")
        logger.error("DB intraday query %s failed: %s", label, reason)
        raise MarketDataUnavailable(label, reason)
    return result.get("data") or []


def late_starting_tickers(
    frame: pd.DataFrame, tickers: list[str], window_start: date
) -> dict[str, date | None]:
    """Tickers whose first bar inside the window trails ``window_start`` too far.

    Bars before ``window_start`` (lookback) do not count as coverage. A ticker
    with no bar inside the window maps to None.
    """
    limit = window_start + timedelta(days=_LATE_START_TOLERANCE_DAYS)
    first_days: dict[str, date] = {}
    if not frame.empty:
        stamps = frame["timestamp"]
        if stamps.dt.tz is not None:
            stamps = stamps.dt.tz_convert(NY_TZ)
        days = stamps.dt.date
        in_window = days >= window_start
        first_days = days[in_window].groupby(frame.loc[in_window, "ticker"]).min().to_dict()
    late: dict[str, date | None] = {}
    for ticker in tickers:
        first = first_days.get(ticker)
        if first is None or first > limit:
            late[ticker] = first
    return late


def spacing_probe_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Short inclusive windows covering the start, every ~60 days, and the end."""
    span = timedelta(days=_SPACING_SAMPLE_DAYS - 1)
    windows = []
    cursor = start
    while cursor <= end:
        windows.append((cursor, min(end, cursor + span)))
        cursor += timedelta(days=_SPACING_SAMPLE_EVERY_DAYS)
    last_start = max(start, end - span)
    if last_start <= windows[-1][1]:
        windows[-1] = (windows[-1][0], end)
    else:
        windows.append((last_start, end))
    return windows


def stored_bar_minutes(db: Any, tickers: list[str], start: date, end: date) -> int | None:
    """The spacing, in minutes, every requested bar must be a multiple of.

    Samples the window with ``spacing_probe_windows`` and returns the least
    common multiple of the spacings found, so one coarse stretch is enough to
    refuse a finer bar. None when no sample holds two rows on one day.
    """
    sql = _SPACING_SQL.format(
        placeholders=", ".join(["%s"] * len(tickers)), session_filter=_SESSION_FILTER
    )
    spacings = []
    for probe_start, probe_end in spacing_probe_windows(start, end):
        lower, upper = _session_bounds(probe_start, probe_end)
        rows = _run_query(db, sql, [*tickers, lower, upper], "intraday spacing")
        if rows:
            spacings.append(max(1, round(float(rows[0]["gap_minutes"]))))
    return math.lcm(*spacings) if spacings else None


def require_resolution(stored: int | None, minutes: int) -> None:
    """Refuse a bar size the stored rows cannot be bucketed into exactly.

    Args:
        stored: Spacing of the stored rows in minutes, or None when unknown.
        minutes: Requested bar length.

    Raises:
        IntradayResolutionUnavailable: ``minutes`` is not a multiple of ``stored``.
    """
    if stored is None or minutes % stored == 0:
        return
    raise IntradayResolutionUnavailable(
        f"The market-data database stores {stored}-minute bars, so {minutes}-minute "
        f"bars cannot be built from it. Choose a bar interval that is a multiple of "
        f"{stored} minutes, or run against MARKET_DATA_SOURCE=fmp."
    )


def fetch_db_intraday_bars(
    db: Any, tickers: list[str], start: date, end: date, minutes: int
) -> pd.DataFrame:
    """Bucket ``market_data`` rows into ``minutes``-long bars for ``[start, end]``.

    Raises:
        IntradayResolutionUnavailable: The table stores coarser bars than requested.
        MarketDataUnavailable: A query did not complete.
    """
    require_resolution(stored_bar_minutes(db, tickers, start, end), minutes)
    parts = []
    slab_start = start
    while slab_start <= end:
        slab_end = min(end, slab_start + timedelta(days=_DB_SLAB_DAYS - 1))
        lower, upper = _session_bounds(slab_start, slab_end)
        for offset in range(0, len(tickers), _DB_TICKER_CHUNK):
            chunk = tickers[offset : offset + _DB_TICKER_CHUNK]
            sql = _BUCKET_SQL.format(
                placeholders=", ".join(["%s"] * len(chunk)), session_filter=_SESSION_FILTER
            )
            rows = _run_query(db, sql, [minutes, *chunk, lower, upper], "intraday bars")
            if rows:
                parts.append(pd.DataFrame(rows))
        slab_start = slab_end + timedelta(days=1)
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    session_open = pd.to_datetime(frame.pop("trade_date")).dt.tz_localize(NY_TZ) + pd.Timedelta(
        hours=9, minutes=30
    )
    offsets = pd.to_timedelta(frame.pop("bucket_index").astype(float) * minutes, unit="min")
    frame["bar_start"] = session_open + offsets
    return _finalize(frame, minutes)


# ---------------------------------------------------------------------------
# FMP source
# ---------------------------------------------------------------------------


def fmp_chunks(start: date, end: date, minutes: int) -> list[tuple[date, date]]:
    """Consecutive inclusive windows no longer than FMP returns for this interval."""
    span = timedelta(days=_FMP_CHUNK_DAYS[minutes] - 1)
    chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + span)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def parse_fmp_intraday_rows(ticker: str, payload: list[dict], minutes: int) -> pd.DataFrame:
    """FMP intraday rows (start-labelled, New York wall time) as close-labelled engine rows.

    Raises:
        FMPUnavailable: A row is malformed or carries a non-positive price.
    """
    records = []
    for raw in payload:
        try:
            bar_start = datetime.fromisoformat(str(raw["date"]))
            prices = {
                f"{field}_price": float(raw[field]) for field in ("open", "high", "low", "close")
            }
            volume = float(raw["volume"])
            if any(not math.isfinite(price) or price <= 0 for price in prices.values()):
                raise ValueError("invalid price")
            if not math.isfinite(volume) or volume < 0:
                raise ValueError("invalid volume")
        except (KeyError, TypeError, ValueError, OverflowError):
            raise FMPUnavailable(
                f"FMP returned an invalid intraday bar for {ticker}. Retry shortly."
            ) from None
        records.append({"ticker": ticker, "bar_start": bar_start, **prices, "volume": volume})
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    frame["bar_start"] = frame["bar_start"].dt.tz_localize(NY_TZ)
    return _finalize(frame, minutes)


def fetch_fmp_intraday_bars(
    tickers: list[str], start: date, end: date, minutes: int
) -> pd.DataFrame:
    """Download ``minutes``-long bars from FMP for ``[start, end]``, chunked per interval."""
    client = FMPMarketData()
    interval = _FMP_INTERVAL_LABELS[minutes]
    jobs = [(ticker, chunk) for ticker in tickers for chunk in fmp_chunks(start, end, minutes)]

    def fetch(job: tuple[str, tuple[date, date]]) -> pd.DataFrame:
        ticker, (chunk_start, chunk_end) = job
        payload = client.get_intraday_history(ticker, interval, chunk_start, chunk_end)
        return parse_fmp_intraday_rows(ticker, payload, minutes)

    with ThreadPoolExecutor(max_workers=min(_FMP_WORKERS, max(len(jobs), 1))) as pool:
        parts = [frame for frame in pool.map(fetch, jobs) if not frame.empty]
    logger.info(
        "FMP | Fetched intraday history; interval=%s tickers=%d window=%s..%s requests=%d",
        interval, len(tickers), start, end, len(jobs),
    )
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    days = frame["timestamp"].dt.date
    frame = frame[(days >= start) & (days <= end)]
    return (
        frame.drop_duplicates(["ticker", "timestamp"], keep="last")
        .sort_values(["timestamp", "ticker"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def fetch_intraday_bars(
    db: Any,
    tickers: list[str],
    start: date | datetime | str,
    end: date | datetime | str,
    minutes: int,
    *,
    require_all: bool = True,
) -> pd.DataFrame:
    """Intraday bars for ``tickers`` over ``[start, end]`` from the configured source.

    Args:
        db: The run's adapter: an ``FMPDataAdapter`` (memoized per run) or a
            SQL adapter exposing ``execute_query``.
        tickers: Symbols to load.
        start: First New York trading date, inclusive.
        end: Last New York trading date, inclusive.
        minutes: Bar length; one of 1, 5, 15, 30, 60.
        require_all: Raise when any ticker has no bars in the window.

    Returns:
        Columns ``ticker``, ``timestamp`` (close-labelled, New York), OHLC and
        ``volume``, sorted by timestamp.

    Raises:
        NoMarketData: ``require_all`` and a ticker returned nothing.
    """
    wanted = list(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))
    first, last = _as_date(start), _as_date(end)
    if not wanted or last < first:
        return pd.DataFrame()
    if isinstance(db, FMPDataAdapter):
        frame = db.get_intraday_history(wanted, first, last, minutes)
    elif market_data_source() == "fmp":
        frame = fetch_fmp_intraday_bars(wanted, first, last, minutes)
    else:
        frame = fetch_db_intraday_bars(db, wanted, first, last, minutes)
    present = set(frame["ticker"]) if not frame.empty else set()
    missing = [ticker for ticker in wanted if ticker not in present]
    if missing and require_all:
        raise NoMarketData(
            missing, first, last, reason=f"no {minutes}-minute bars in this window"
        )
    return frame
