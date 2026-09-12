"""The starter strategy handed to someone opening the editor.

It lives here, beside the scan that judges it, for one reason: a template that
does not pass our own compatibility check teaches the wrong contract on the
first screen a member sees. A test asserts exactly that, so the two cannot
drift.

Written against the vendored engine rather than from memory. ``OnData(self,
context)`` is the exact spelling the run loop calls, the universe comes from
``self.tickers`` (the backend generates the config), and the indicators are
names ``engine/indicators`` actually ships.
"""

from __future__ import annotations

STARTER_FILENAME = "strategy.py"

STARTER_SOURCE = '''import logging

from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class MyStrategy(BasePortfolio):
    """One sentence on what edge this is trying to capture."""

    def __init__(
        self,
        db_connector,
        executor,
        debug=False,
        config_dict=None,
        backtest_start_date=None,
        order_manager=None,
    ):
        # BasePortfolio reads the config. self.tickers, self.lookback_days and
        # the indicator machinery all come out of this call.
        super().__init__(
            db_connector, executor, debug, config_dict, backtest_start_date, order_manager
        )
        self.logger = logging.getLogger(self.__class__.__name__)

        # "attribute_name": ("IndicatorName", {parameters})
        self.RegisterIndicatorSet({
            "fast_sma": ("SimpleMovingAverage", {"period": 20}),
            "slow_sma": ("SimpleMovingAverage", {"period": 50}),
        })

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
'''
