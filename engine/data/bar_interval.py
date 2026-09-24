"""Bar sizes a run may use, and the limits on intraday windows.

VISUALIZER: kept free of pandas and numpy on purpose. The API process imports
these rules to validate a submission, and stamping a run must not pay for the
data stack; the loaders that need pandas live in ``engine.data.intraday``.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from engine.contracts.errors import EngineError

DAILY_BAR_SECONDS = 86_400
# The run form's choices. Anything else is refused rather than rounded.
BAR_SECONDS_CHOICES: tuple[int, ...] = (60, 300, 900, 1_800, 3_600, DAILY_BAR_SECONDS)
SESSION_MINUTES = 390
# Bars loaded, lookback included. Measured 2026-09-23: portfolio_1's config
# (5 tickers, 90-day lookback) on FMP 1-minute bars for July 2026 loaded about
# 168k bars and simulated 8,580 steps in 524 s, because every step hands the
# strategy its whole lookback slice. Projected from that run, this limit keeps
# the same config under about 25 minutes (derived, not measured).
MAX_INTRADAY_BARS = 250_000

_DAYS_PER_WEEK = 7
_WEEKDAYS_PER_WEEK = 5


class IntradayWindowTooLarge(EngineError):
    """The requested window would load more intraday bars than a run may hold."""


def bar_minutes(bar_seconds: Any) -> int:
    """Validate a bar size in seconds and return it in minutes.

    Args:
        bar_seconds: One of ``BAR_SECONDS_CHOICES``.

    Returns:
        The bar length in minutes; 1440 for a daily bar.

    Raises:
        ValueError: The value is not one of the supported bar sizes.
    """
    if isinstance(bar_seconds, bool) or bar_seconds not in BAR_SECONDS_CHOICES:
        allowed = ", ".join(str(choice) for choice in BAR_SECONDS_CHOICES)
        raise ValueError(f"Bar interval must be one of {allowed} seconds; got {bar_seconds!r}.")
    return int(bar_seconds) // 60


def is_intraday(bar_seconds: Any) -> bool:
    """True for any supported bar size shorter than one trading day.

    Raises:
        ValueError: The value is not one of the supported bar sizes.
    """
    return bar_minutes(bar_seconds) < SESSION_MINUTES


def bars_per_session(minutes: int) -> int:
    """Bars in one regular 09:30–16:00 session; the last bar may be short."""
    return math.ceil(SESSION_MINUTES / minutes)


def weekdays_between(start: date, end: date) -> int:
    """Monday-to-Friday dates in ``[start, end]``; zero when ``end`` precedes ``start``."""
    if end < start:
        return 0
    full_weeks, remainder = divmod((end - start).days + 1, _DAYS_PER_WEEK)
    first_weekday = start.weekday()
    partial = sum(
        1 for offset in range(remainder)
        if (first_weekday + offset) % _DAYS_PER_WEEK < _WEEKDAYS_PER_WEEK
    )
    return full_weeks * _WEEKDAYS_PER_WEEK + partial


def estimate_bar_count(ticker_count: int, start: date, end: date, minutes: int) -> int:
    """Upper bound on the bars a window loads: weekdays × bars per session × tickers.

    Holidays are counted as sessions, so the estimate errs high.
    """
    return ticker_count * weekdays_between(start, end) * bars_per_session(minutes)


def check_intraday_size(ticker_count: int, start: date, end: date, minutes: int) -> None:
    """Refuse a window whose intraday bar count exceeds ``MAX_INTRADAY_BARS``.

    Raises:
        IntradayWindowTooLarge: With the estimate and what to change.
    """
    estimate = estimate_bar_count(ticker_count, start, end, minutes)
    if estimate > MAX_INTRADAY_BARS:
        raise IntradayWindowTooLarge(
            f"A {minutes}-minute run over {start}..{end} for {ticker_count} ticker(s) "
            f"would load about {estimate:,} bars (limit {MAX_INTRADAY_BARS:,}, "
            "including any strategy lookback). Choose a larger bar interval, "
            "a shorter window or fewer tickers."
        )


def warmup_calendar_days(period: int, minutes: int) -> int:
    """Calendar days of history that cover ``period`` bars of ``minutes`` each.

    Sessions are converted to calendar days at 7/5 with a 20% holiday margin,
    plus four days so a Monday start still reaches the previous week.
    """
    sessions = math.ceil(max(period, 1) / bars_per_session(minutes))
    return math.ceil(sessions * 7 / 5 * 1.2) + 4
