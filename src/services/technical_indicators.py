"""Pure indicator math for ``GET /indicators``.

No I/O: every function takes daily closes (oldest first) or article scores and
returns numbers, so the arithmetic is tested without a database.

Conventions are the textbook ones the dashboard labels promise: Wilder's RSI(14),
MACD(12, 26, 9) histogram in price units, the 50-day SMA against the 200-day, and
20-session momentum as a ratio.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

RSI_PERIOD = 14
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
SMA_FAST_PERIOD = 50
SMA_SLOW_PERIOD = 200
MOMENTUM_PERIOD = 20
SENTIMENT_WINDOW = timedelta(days=7)

# Every indicator above can be computed once this many closes exist; the SMA
# regime is the longest look-back.
MIN_CLOSES = SMA_SLOW_PERIOD

_RSI_MAX = 100.0
_RSI_MIN = 0.0


def _require_length(closes: Sequence[float], needed: int, name: str) -> None:
    if len(closes) < needed:
        raise ValueError(f"{name} needs at least {needed} closes, got {len(closes)}.")


def simple_moving_average(closes: Sequence[float], period: int) -> float:
    """Mean of the last ``period`` closes.

    Raises:
        ValueError: fewer than ``period`` closes.
    """
    _require_length(closes, period, f"SMA({period})")
    return sum(closes[-period:]) / period


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    """EMA seeded with the SMA of the first ``period`` values, one point per value after."""
    smoothing = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    series = [ema]
    for value in values[period:]:
        ema = value * smoothing + ema * (1.0 - smoothing)
        series.append(ema)
    return series


def relative_strength_index(closes: Sequence[float], period: int = RSI_PERIOD) -> float:
    """Wilder's RSI over the whole series, in [0, 100].

    A series with no losses is 100 and one with no gains is 0; a flat series,
    which has neither, is the neutral 50.

    Raises:
        ValueError: fewer than ``period + 1`` closes.
    """
    _require_length(closes, period + 1, f"RSI({period})")
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    average_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    average_loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period

    if average_loss == 0.0:
        return _RSI_MAX if average_gain > 0.0 else _RSI_MAX / 2
    relative_strength = average_gain / average_loss
    rsi = _RSI_MAX - _RSI_MAX / (1.0 + relative_strength)
    return min(max(rsi, _RSI_MIN), _RSI_MAX)


def macd_histogram(
    closes: Sequence[float],
    fast: int = MACD_FAST_PERIOD,
    slow: int = MACD_SLOW_PERIOD,
    signal: int = MACD_SIGNAL_PERIOD,
) -> float:
    """MACD line minus its signal line at the last close, in price units.

    Raises:
        ValueError: fewer than ``slow + signal - 1`` closes.
    """
    _require_length(closes, slow + signal - 1, f"MACD({fast},{slow},{signal})")
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    # Align on the closes where both EMAs exist: the slow one starts later.
    offset = slow - fast
    macd_line = [fast_value - slow_value
                 for fast_value, slow_value in zip(fast_series[offset:], slow_series)]
    signal_series = _ema_series(macd_line, signal)
    return macd_line[-1] - signal_series[-1]


def sma_regime(closes: Sequence[float]) -> Literal["above", "below"]:
    """Whether the 50-day SMA sits above the 200-day. A tie counts as ``below``.

    Raises:
        ValueError: fewer than 200 closes.
    """
    fast = simple_moving_average(closes, SMA_FAST_PERIOD)
    slow = simple_moving_average(closes, SMA_SLOW_PERIOD)
    return "above" if fast > slow else "below"


def momentum(closes: Sequence[float], period: int = MOMENTUM_PERIOD) -> float:
    """Return over the last ``period`` sessions as a ratio (0.031 is +3.1%).

    Raises:
        ValueError: fewer than ``period + 1`` closes, or a zero base close.
    """
    _require_length(closes, period + 1, f"momentum({period})")
    base = closes[-period - 1]
    if base == 0.0:
        raise ValueError("Momentum is undefined against a zero close.")
    return closes[-1] / base - 1.0


def sentiment_window_scores(
    articles: Sequence[tuple[datetime, float]], window_end: datetime
) -> tuple[float, float]:
    """Mean article score for the 7 days ending at ``window_end``, and its change.

    Every article counts once. The change is the current window's mean minus the
    mean of the 7 days before it. A window with no articles scores the neutral
    0.0, because the dashboard's schema has no way to say "no coverage".

    Args:
        articles: ``(published_at, score)`` pairs; order does not matter.
        window_end: The instant the current window closes (inclusive).

    Returns:
        ``(sentiment_7d, sentiment_delta_7d)``, the first clamped to [-1, 1].
    """
    current_start = window_end - SENTIMENT_WINDOW
    prior_start = current_start - SENTIMENT_WINDOW
    current = [score for published, score in articles
               if current_start < published <= window_end]
    prior = [score for published, score in articles
             if prior_start < published <= current_start]
    current_mean = sum(current) / len(current) if current else 0.0
    prior_mean = sum(prior) / len(prior) if prior else 0.0
    return min(max(current_mean, -1.0), 1.0), current_mean - prior_mean
