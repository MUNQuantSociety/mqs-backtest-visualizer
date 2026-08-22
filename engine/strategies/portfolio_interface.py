import pandas as pd

from engine.strategies import toolkit  # noqa: F401  (registers .toolkit accessor)


class PortfolioManager:
    """
    Provides a clean, high-level interface to the current state of the portfolio.
    """

    def __init__(self, cash: float, total_value: float, positions_df: pd.DataFrame):
        self.cash = float(cash)
        self.total_value = float(total_value)

        if positions_df is not None and not positions_df.empty:
            self.positions = dict(zip(positions_df["ticker"], positions_df["quantity"]))
        else:
            self.positions = {}

    def get_asset_value(self, ticker: str, current_price: float) -> float:
        quantity = float(self.positions.get(ticker, 0.0))
        return quantity * current_price

    def get_asset_weight(self, ticker: str, current_price: float) -> float:
        total_val = float(self.total_value)
        if total_val == 0:
            return 0.0
        asset_value = float(self.get_asset_value(ticker, current_price))
        return asset_value / total_val

    def __repr__(self) -> str:
        return f"PortfolioManager(TotalValue={self.total_value:,.2f}, Cash={self.cash:,.2f}, Positions={len(self.positions)})"
