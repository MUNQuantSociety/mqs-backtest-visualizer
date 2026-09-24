"""Point-in-time news-sentiment gate for backtest long entries.

VISUALIZER: not vendored from MQSMaster. A run with the gate on may not add
long exposure to a ticker while that ticker's 7-day mean article score is below
the threshold. Selling, trimming a long and covering a short are never blocked.

Pure data and arithmetic: the worker loads the article scores before the run
and hands them in, so the engine never touches a database.

Look-ahead rules, from the MQSMaster NLP look-ahead audit (D3):

* An article counts only once it is *available*, strictly before the bar.
* ``published_at`` is stored naive and treated as UTC, but some vendors (FMP)
  send Eastern time, which makes an article look up to five hours older than
  it is. Every article is therefore embargoed for five hours.
* Date-only sources are stored at exactly midnight. Such an article could have
  appeared at any time that day, so it counts from the end of that day.

A window with no available articles scores the neutral 0.0, the same rule the
dashboard uses; with a threshold of at most 0 that never blocks.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

SENTIMENT_WINDOW = timedelta(days=7)
VENDOR_TIMEZONE_EMBARGO = timedelta(hours=5)
DATE_ONLY_EMBARGO = timedelta(days=1)
THRESHOLD_MIN = -1.0
THRESHOLD_MAX = 0.0


def available_at(published_at: datetime) -> datetime:
    """When an article may first influence a bar, as naive UTC."""
    if published_at.time() == time.min:
        return published_at + DATE_ONLY_EMBARGO
    return published_at + VENDOR_TIMEZONE_EMBARGO


def _to_naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class SentimentGate:
    """Blocks new long exposure while a ticker's recent news is too negative.

    Args:
        threshold: Block when the 7-day mean score is strictly below this, in
            [-1, 0].
        articles: ``(published_at, score)`` pairs per ticker; ``published_at``
            naive UTC as stored.

    Raises:
        ValueError: a threshold outside [-1, 0].
    """

    threshold: float
    articles: Mapping[str, Iterable[tuple[datetime, float]]]
    blocked_entry_count: int = field(default=0, init=False)
    _times: dict[str, list[datetime]] = field(default_factory=dict, init=False, repr=False)
    _prefix: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not THRESHOLD_MIN <= self.threshold <= THRESHOLD_MAX:
            raise ValueError(
                f"Sentiment gate threshold must be between {THRESHOLD_MIN:g} and {THRESHOLD_MAX:g}."
            )
        for ticker, rows in self.articles.items():
            ordered = sorted((available_at(published), float(score)) for published, score in rows)
            self._times[ticker.upper()] = [moment for moment, _ in ordered]
            running = [0.0]
            for _, score in ordered:
                running.append(running[-1] + score)
            self._prefix[ticker.upper()] = running

    def score_at(self, ticker: str, moment: datetime) -> float:
        """Mean score of articles available in the 7 days strictly before ``moment``."""
        times = self._times.get(ticker.upper())
        if not times:
            return 0.0
        now = _to_naive_utc(moment)
        end = bisect_left(times, now)
        start = bisect_left(times, now - SENTIMENT_WINDOW)
        count = end - start
        if count <= 0:
            return 0.0
        prefix = self._prefix[ticker.upper()]
        return (prefix[end] - prefix[start]) / count

    def blocks_long_entry(self, ticker: str, moment: datetime) -> bool:
        """True when new long exposure in ``ticker`` is not allowed at ``moment``."""
        return self.score_at(ticker, moment) < self.threshold

    def record_block(self) -> None:
        """Count one sizing call the gate reduced."""
        self.blocked_entry_count += 1

    def coverage(self) -> dict[str, dict[str, object]]:
        """Per ticker: articles held and when the first one became available."""
        return {
            ticker: {
                "articleCount": len(times),
                "firstAvailable": times[0].replace(tzinfo=timezone.utc).isoformat() if times else None,
            }
            for ticker, times in sorted(self._times.items())
        }

    def report(self) -> dict[str, object]:
        """What the run report records about the gate."""
        return {
            "enabled": True,
            "threshold": self.threshold,
            "window": "7d",
            "blockedEntryCount": self.blocked_entry_count,
            "coverage": self.coverage(),
        }
