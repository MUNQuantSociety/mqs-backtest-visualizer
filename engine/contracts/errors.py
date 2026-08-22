"""Engine failures the caller is expected to distinguish.

Everything else that goes wrong inside a run is an ordinary exception and ends
up as ``RunResult.status == "failed"`` with its class name in ``error``. These
are different because the reader reacts differently: a cancellation is a user
action, missing market data is a bad request rather than a bug, and a database
that could not be reached is neither — it is worth retrying unchanged.
"""

from __future__ import annotations

from typing import Any, Sequence


class EngineError(Exception):
    """Base class for every error the engine raises deliberately."""


class RunCancelled(EngineError):
    """The run stopped because ``should_cancel()`` returned True."""


class NoMarketData(EngineError):
    """No usable bars exist for the requested tickers and window.

    The tickers and the window are carried as attributes *and* interpolated
    into the message, because this string is what a student reads in the
    browser when a run fails — "no data" alone is not actionable.
    """

    def __init__(
        self,
        tickers: Sequence[str],
        start: Any,
        end: Any,
        reason: str | None = None,
    ) -> None:
        self.tickers = list(tickers)
        self.start = start
        self.end = end
        ticker_list = ", ".join(self.tickers) if self.tickers else "<none>"
        detail = reason or "no market data"
        super().__init__(
            f"{detail} for [{ticker_list}] between {start} and {end}. "
            "Check the ticker coverage of public.market_data for this window."
        )


class MarketDataUnavailable(EngineError):
    """A market-data query failed — the answer is unknown, not empty.

    This exists so the empty-frame guard cannot take the blame for a database
    outage. ``fetch_historical_data`` used to return an empty DataFrame both
    when a window genuinely held no bars and when the query itself errored,
    which made a dropped university-network connection read as
    ``NoMarketData: ... Check the ticker coverage of public.market_data`` — a
    student rewriting a perfectly good date window because the message told
    them to.
    """

    def __init__(self, label: str, reason: str) -> None:
        self.label = label
        self.reason = reason
        super().__init__(
            f"the market-data query ({label}) did not complete: {reason}. "
            "This is a database or network failure, not a gap in the data — "
            "the same run is worth retrying unchanged."
        )
