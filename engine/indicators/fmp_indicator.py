"""An indicator whose values come from FMP's technical-indicator endpoint.

Registered like any other indicator::

    INDICATORS = {"rsi_14": ("FmpIndicator", {"name": "rsi", "period": 14})}

Instead of computing from the bars it is fed, it reads FMP's published value
for the latest session that has closed, so a strategy trades on the same
numbers a chart drawn from that endpoint shows.

Only one class may live at module level here: the strategy scanner lists every
top-level class in ``engine/indicators`` as an indicator a strategy can name.
"""

from __future__ import annotations

import bisect
import math
from datetime import date, time, timedelta

import pandas as pd

from engine.data.fmp import (
    TECHNICAL_INDICATOR_MAX_PERIOD,
    TECHNICAL_INDICATOR_MIN_PERIOD,
    TECHNICAL_INDICATORS,
    FMPMarketData,
)
from engine.indicators.base import Indicator

_NEW_YORK = "America/New_York"
SESSION_CLOSE = time(16, 0)

# A value older than this is treated as missing rather than carried forward.
STALE_AFTER_DAYS = 7

# FMP seeds each calculation near the requested start, so every request begins
# this many calendar days before the first day it is read for. About six
# periods of sessions settles EMA-style smoothing (EMA, RSI, ADX); the ceiling
# keeps a request inside FMP's ~1,250-row answer together with CHUNK_DAYS.
WARMUP_MIN_DAYS = 400
WARMUP_MAX_DAYS = 700
CHUNK_DAYS = 1000


def warmup_days(period: int) -> int:
    """Calendar days requested before the first day a chunk is read for."""
    return min(max(WARMUP_MIN_DAYS, math.ceil(period * 9)), WARMUP_MAX_DAYS)


def completed_session(timestamp) -> date:
    """The last trading day whose close is known at ``timestamp``.

    A daily bar is labelled at the 16:00 New York close and reads its own day.
    Anything earlier in the day (an intraday bar, a midnight label) reads the
    day before, so no bar ever sees a close that has not happened yet. Naive
    timestamps are taken as New York time.
    """
    moment = pd.Timestamp(timestamp)
    moment = moment.tz_localize(_NEW_YORK) if moment.tzinfo is None else moment.tz_convert(_NEW_YORK)
    day = moment.date()
    return day if moment.time() >= SESSION_CLOSE else day - timedelta(days=1)


def load_series(client, ticker: str, name: str, period: int, first: date, last: date) -> dict[date, float]:
    """FMP values for ``first``..``last``, fetched in warmed-up chunks."""
    values: dict[date, float] = {}
    warmup = timedelta(days=warmup_days(period))
    chunk_start = first
    while chunk_start <= last:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), last)
        rows = client.get_technical_indicator(ticker, name, period, chunk_start - warmup, chunk_end)
        values.update((day, value) for day, value in rows if day >= chunk_start)
        chunk_start = chunk_end + timedelta(days=1)
    return values


class FmpIndicator(Indicator):
    """One FMP indicator series (``name``, ``period``) for one ticker.

    ``name`` is FMP's: sma, ema, wma, dema, tema, rsi, standarddeviation,
    williams or adx. The whole series from the first bar up to today is
    downloaded on the first update — normally during warmup, while the
    strategy is being built, so a provider failure fails the run up front.
    A session FMP has no value for leaves the indicator not ready.
    """

    def __init__(self, ticker: str, **kwargs):
        super().__init__(ticker=ticker, **kwargs)
        self.name = str(kwargs.get("name", "sma")).strip().lower()
        self.period = int(kwargs.get("period", 14))
        self.price_col = kwargs.get("price_col", "close_price")

        if self.name not in TECHNICAL_INDICATORS:
            raise ValueError(
                f"FmpIndicator name must be one of {', '.join(TECHNICAL_INDICATORS)}, got {self.name!r}."
            )
        if not TECHNICAL_INDICATOR_MIN_PERIOD <= self.period <= TECHNICAL_INDICATOR_MAX_PERIOD:
            raise ValueError(
                f"FmpIndicator period must be {TECHNICAL_INDICATOR_MIN_PERIOD}-"
                f"{TECHNICAL_INDICATOR_MAX_PERIOD}, got {self.period}."
            )

        self._client: FMPMarketData | None = None
        self._values: dict[date, float] = {}
        self._days: list[date] = []
        self._loaded: tuple[date, date] | None = None

    def Update(self, timestamp, data_point=None, **kwargs):
        session = completed_session(timestamp)
        self._ensure_loaded(session)
        value = self._value_as_of(session)
        self._current_value = value
        self._is_ready = value is not None

    def _ensure_loaded(self, session: date) -> None:
        needed_from = session - timedelta(days=STALE_AFTER_DAYS)
        if self._loaded is None:
            today = pd.Timestamp.now(tz=_NEW_YORK).date()
            self._load(needed_from, max(session, today))
            return
        loaded_from, loaded_through = self._loaded
        if needed_from < loaded_from:
            self._load(needed_from, loaded_from - timedelta(days=1))
        if session > loaded_through:
            self._load(loaded_through + timedelta(days=1), session)

    def _load(self, first: date, last: date) -> None:
        if self._client is None:
            self._client = FMPMarketData()
        self._values.update(load_series(self._client, self.ticker, self.name, self.period, first, last))
        self._days = sorted(self._values)
        if self._loaded is None:
            self._loaded = (first, last)
        else:
            self._loaded = (min(first, self._loaded[0]), max(last, self._loaded[1]))

    def _value_as_of(self, session: date) -> float | None:
        index = bisect.bisect_right(self._days, session) - 1
        if index < 0:
            return None
        day = self._days[index]
        if (session - day).days > STALE_AFTER_DAYS:
            return None
        return self._values[day]
