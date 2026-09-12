"""The starter strategy handed to someone opening the editor.

It lives here, beside the scan that judges it, for one reason: a template that
does not pass our own compatibility check teaches the wrong contract on the
first screen a member sees. A test asserts exactly that, so the two cannot
drift.

Written against the vendored engine rather than from memory. ``OnData(self,
context)`` is the exact spelling the run loop calls, the universe comes from
``self.tickers`` (the backend generates the config), and the indicators are
names ``engine/indicators`` actually ships.

It teaches the declarative shape on purpose: ``INDICATORS`` and ``STATE`` are
class attributes ``BasePortfolio.__init__`` reads, so the only method a member
writes is ``OnData``. Writing an explicit ``__init__`` that calls
``super().__init__(...)`` and ``RegisterIndicatorSet`` still works — the first
screen should just not be the one that teaches boilerplate.
"""

from __future__ import annotations

STARTER_FILENAME = "strategy.py"

STARTER_SOURCE = '''from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class MyStrategy(BasePortfolio):
    """One sentence on what edge this is trying to capture."""

    # "attribute_name": ("IndicatorName", {parameters}). One instance per
    # ticker, so self.fast_sma[ticker] is the indicator for that ticker.
    # BasePortfolio builds and warms them before the first bar; self.tickers,
    # self.lookback_days and self.logger are ready by then too.
    INDICATORS = {
        "fast_sma": ("SimpleMovingAverage", {"period": 20}),
        "slow_sma": ("SimpleMovingAverage", {"period": 50}),
    }

    # Anything you want to remember between bars: "attribute_name": default.
    # Each default is copied per run, so self.last_price is your own dict.
    STATE = {"last_price": {}}

    def OnData(self, context: StrategyContext):
        """Called once per bar. Trade through `context`; return nothing."""
        for ticker in self.tickers:
            asset = context.Market[ticker]
            fast = self.fast_sma[ticker]
            slow = self.slow_sma[ticker]

            # Indicators need their full period before they mean anything.
            if not (asset.Exists and fast.IsReady and slow.IsReady):
                continue

            holding = context.Portfolio.positions.get(ticker, 0)

            if fast.Current > slow.Current and holding <= 0:
                context.buy(ticker, confidence=1.0)
            elif fast.Current < slow.Current and holding > 0:
                context.sell(ticker, confidence=1.0)

            # Whatever you put in STATE is yours to keep across bars.
            self.last_price[ticker] = asset.Close
'''
