"""Deterministic source uploaded by the disposable-Postgres pipeline test."""

from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class DisposablePipelineStrategy(BasePortfolio):
    def __init__(
        self,
        db_connector,
        executor,
        debug=False,
        config_dict=None,
        backtest_start_date=None,
        order_manager=None,
    ):
        super().__init__(
            db_connector, executor, debug, config_dict, backtest_start_date, order_manager
        )
        self.steps = 0

    def OnData(self, context):
        self.steps += 1
        for ticker in self.tickers:
            if self.steps % 8 == 2:
                context.buy(ticker, confidence=1.0)
            elif self.steps % 8 == 6:
                context.sell(ticker, confidence=1.0)
