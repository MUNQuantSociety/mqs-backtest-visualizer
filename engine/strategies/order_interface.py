# engine/strategies/order_interface.py  (vendored from MQSMaster)
import logging
import math
from collections.abc import Mapping
from datetime import datetime

import pandas as pd
import pytz as pytz

from engine.strategies import toolkit  # noqa: F401  (registers .toolkit accessor)
from engine.strategies.market_data_api import MarketData
from engine.strategies.portfolio_interface import PortfolioManager


class StrategyContext:
    """
    The master context object passed to the strategy's OnData method on each time step.
    It encapsulates MarketData, PortfolioManager, and provides trade execution methods
    """

    def __init__(
        self,
        market_data_df,
        cash_df,
        positions_df,
        port_notional_df,
        current_time,
        executor,
        portfolio_config,
        order_manager=None,
    ):
        self._executor = executor
        self._order_manager = order_manager
        self._portfolio_config = portfolio_config
        self._positions_df = positions_df
        effective_time = current_time
        timezone = pytz.timezone("America/New_York")
        if effective_time is None:
            if (
                market_data_df is not None
                and not getattr(market_data_df, "empty", True)
                and "timestamp" in market_data_df.columns
            ):
                try:
                    effective_time = pd.to_datetime(market_data_df["timestamp"]).max()
                except Exception:
                    effective_time = datetime.now(timezone)
            else:
                effective_time = datetime.now(timezone)
        self.time = effective_time

        # Initialize the high-level helper classes
        self.Market = MarketData(market_data_df, effective_time)

        cash_val = (
            cash_df.iloc[0]["notional"]
            if cash_df is not None and not cash_df.empty
            else 0.0
        )

        port_val = (
            port_notional_df.iloc[0]["notional"]
            if port_notional_df is not None and not port_notional_df.empty
            else 0.0
        )

        self.Portfolio = PortfolioManager(
            cash=cash_val, total_value=port_val, positions_df=positions_df
        )

    def buy(self, ticker: str, confidence: float = 1.0):
        """Move toward the configured long allocation, covering a short first."""
        self.target_weight(ticker, self._allocation_weight(ticker), confidence)

    def sell(self, ticker: str, confidence: float = 1.0):
        """Reduce an existing long toward flat; never open or enlarge a short."""
        positions = getattr(self._executor, "positions", self.Portfolio.positions)
        if positions.get(ticker, 0.0) > 0:
            self._trade(ticker, "SELL", confidence, target_weight=0.0)

    def close_position(self, ticker: str, confidence: float = 1.0):
        """Reduce either a long or short position toward flat."""
        self.target_weight(ticker, 0.0, confidence)

    def target_weight(self, ticker: str, weight: float, confidence: float = 1.0):
        """Move toward signed portfolio exposure; negative weights intentionally short."""
        weight = float(weight)
        if not math.isfinite(weight):
            raise ValueError("Target weight must be finite")
        self._trade(ticker, "BUY", confidence, target_weight=weight)

    def _allocation_weight(self, ticker: str) -> float:
        # BasePortfolio passes the existing WEIGHTS/TICKERS config under these
        # lowercase keys. Absent weights mean equal allocation; explicit zero
        # or omitted mapping entries remain unallocated, as in reporting.
        tickers = list(dict.fromkeys(self._portfolio_config.get("tickers", [])))
        if ticker not in tickers:
            return 0.0
        weights = self._portfolio_config.get("weights")
        if weights is None:
            return 1.0 / len(tickers)
        if isinstance(weights, Mapping):
            weight = weights.get(ticker, 0.0)
        elif isinstance(weights, (list, tuple)) and len(weights) == len(tickers):
            weight = weights[tickers.index(ticker)]
        else:
            raise ValueError("WEIGHTS must be a mapping or one weight per ticker")
        weight = float(weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("WEIGHTS must contain finite nonnegative numbers")
        return weight

    def _trade(self, ticker: str, signal_type: str, confidence: float, *, target_weight: float):
        asset_data = self.Market[ticker]
        if not asset_data.Exists or asset_data.Close is None or asset_data.Close <= 0:
            logging.warning(
                "Skip trade: no valid market data for %s at %s (Exists=%s, Close=%s)",
                ticker,
                self.time,
                asset_data.Exists,
                asset_data.Close,
            )
            return

        if self._order_manager is not None:
            # OMS path: size with the executor's default model, then register the
            # order with the OMS (which owns execution from here).
            sizing = self._executor.default_trade_size(
                portfolio_id=self._portfolio_config["id"],
                signal_type=signal_type,
                ticker=ticker,
                arrival_price=asset_data.Close,
                confidence=confidence,
                cash=self.Portfolio.cash,
                positions=self._positions_df,
                port_notional=self.Portfolio.total_value,
                ticker_weight=0.0,
                target_weight=target_weight,
            )
            if sizing.quantity > 0:
                # Direction comes from the SIGN of the sized notional, not
                # the raw signal — identical to execute_trade's settlement
                # rule (see CLAUDE.md "Trade direction"). A BUY signal on an
                # over-weighted position sizes negative and must be worked
                # as a SELL (trim), and vice versa.
                execution_side = "SELL" if sizing.desired_notional < 0 else "BUY"
                try:
                    self._order_manager.process_order(
                        portfolio_id=self._portfolio_config["id"],
                        ticker=ticker,
                        side=execution_side,
                        confidence=confidence,
                        arrival_price=asset_data.Close,
                        total_quantity=sizing.quantity,
                        timestamp=self.time,
                    )
                except Exception as e:
                    logging.error(
                        "OrderManager.process_order failed for %s: %s", ticker, e
                    )
        else:
            # Direct execution: the executor sizes and fills in one call.
            self._executor.execute_trade(
                portfolio_id=self._portfolio_config["id"],
                ticker=ticker,
                signal_type=signal_type,
                confidence=confidence,
                arrival_price=asset_data.Close,
                cash=self.Portfolio.cash,
                positions=self._positions_df,
                port_notional=self.Portfolio.total_value,
                ticker_weight=0.0,
                target_weight=target_weight,
                timestamp=self.time,
            )
