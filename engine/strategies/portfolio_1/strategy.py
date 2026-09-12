import logging
import math

from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class VolMomentum(BasePortfolio):
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
            db_connector,
            executor,
            debug,
            config_dict,
            backtest_start_date,
            order_manager,
        )
        self.logger = logging.getLogger(
            f"{self.__class__.__name__}_{self.portfolio_id}"
        )
        # Format: "indicator_variable_name": ("IndicatorName", {params})
        indicator_definitions = {
            "roc": ("RateOfChange", {"period": 20}),
        }
        self.RegisterIndicatorSet(indicator_definitions)
        self.strategy_diagnostics = self._new_diagnostics()

    @staticmethod
    def _new_diagnostics():
        # Fixed-size counters and one snapshot per ticker; never retain bars.
        return {
            "strategy": "VolMomentum",
            "volatilityAnnualizationDays": 252,
            "volatilityMultiplier": 1.5,
            "minimumReturnObservations": 20,
            "evaluationCount": 0,
            "warmupSkipCount": 0,
            "missingMarketDataSkipCount": 0,
            "bullishSignalCount": 0,
            "bearishSignalCount": 0,
            "buyRequestCount": 0,
            "sellRequestCount": 0,
            "tickers": {},
        }

    @staticmethod
    def _momentum_strength(snapshot):
        if snapshot["thresholdPct"]:
            return snapshot["momentumPct"] / snapshot["thresholdPct"]
        # A zero-volatility window can be valid. Only the comparison key uses
        # infinity; report metadata always contains finite numbers or null.
        momentum = snapshot["momentumPct"]
        return math.copysign(math.inf, momentum) if momentum else 0.0

    def _count_diagnostic(self, ticker_diagnostics, key):
        self.strategy_diagnostics[key] += 1
        ticker_diagnostics[key] += 1

    def OnData(self, context: StrategyContext):
        """Generates BUY, SELL, and HOLD signals based on momentum and volatility, updates cash available for trade, and then calls the trade execution logic for each signal."""
        portfolio = context.Portfolio
        is_risk_off = portfolio.cash < (float(portfolio.total_value) * 0.10)
        if is_risk_off:
            self.logger.info(
                "Risk-Off Mode: Cash is low. No new long positions will be opened."
            )

        # ? A loop to iterate through each ticker and generate signals based on momentum and volatility.
        for ticker in self.tickers:
            asset = context.Market[ticker]
            roc = self.roc[ticker]
            vol_multiplier = 1.5  # This can be adjusted or made configurable
            diagnostics = self.strategy_diagnostics["tickers"].setdefault(ticker, {
                "evaluationCount": 0,
                "warmupSkipCount": 0,
                "missingMarketDataSkipCount": 0,
                "bullishSignalCount": 0,
                "bearishSignalCount": 0,
                "buyRequestCount": 0,
                "sellRequestCount": 0,
                "strongestMomentum": None,
            })

            if not all([asset.Exists, roc.IsReady]):
                self._count_diagnostic(
                    diagnostics,
                    "missingMarketDataSkipCount" if not asset.Exists else "warmupSkipCount",
                )
                continue

            return_history = asset.History("60d")
            # History is a calendar window of daily bars. A 60-row change in
            # this window has no observations and silently makes every signal
            # false. Estimate volatility from consecutive daily returns.
            returns = return_history["close_price"].pct_change(fill_method=None).dropna()
            if len(returns) < 20:
                self._count_diagnostic(diagnostics, "warmupSkipCount")
                continue
            # RateOfChange.Current is expressed in percent, so volatility must
            # use percent as well before comparing the two quantities.
            volatility = float(returns.std()) * (252**0.5) * 100.0

            momentum = roc.Current
            if momentum is None or not math.isfinite(momentum) or not math.isfinite(volatility):
                raise ValueError(f"VolMomentum: non-finite momentum or volatility for {ticker}.")
            threshold = volatility * vol_multiplier
            position = portfolio.positions.get(ticker, 0)

            bullish = momentum > threshold
            bearish = momentum < -threshold
            self._count_diagnostic(diagnostics, "evaluationCount")
            if bullish:
                self._count_diagnostic(diagnostics, "bullishSignalCount")
            if bearish:
                self._count_diagnostic(diagnostics, "bearishSignalCount")
            snapshot = {
                "date": context.time.date().isoformat(),
                "momentumPct": float(momentum),
                "thresholdPct": float(threshold),
                "momentumToThresholdRatio": float(momentum / threshold) if threshold else None,
            }
            strongest = diagnostics["strongestMomentum"]
            if strongest is None or self._momentum_strength(snapshot) > self._momentum_strength(strongest):
                diagnostics["strongestMomentum"] = snapshot

            if bullish and not is_risk_off:
                is_risk_off = False

            weight = 0.2 if bullish else 0.0
            asset_weight = 0.0
            if asset.Exists:
                asset_weight = portfolio.get_asset_weight(ticker, asset.Close)
            if asset_weight <= weight:
                target_weight = True
            elif asset_weight > weight:
                target_weight = False

            if (bullish and target_weight) or position < 0:  # Max 25% weight
                self.logger.debug(
                    f"[{ticker}] BUY signal: momentum ({momentum:.4f}) > threshold ({threshold:.4f}), position={position}"
                )
                self._count_diagnostic(diagnostics, "buyRequestCount")
                context.buy(ticker, confidence=1.0)

            elif position > 0 and (bearish or target_weight is False or is_risk_off):
                self.logger.debug(
                    f"[{ticker}] SELL signal: momentum ({momentum:.4f}) < threshold ({threshold:.4f}), position={position}"
                )
                self._count_diagnostic(diagnostics, "sellRequestCount")
                context.sell(ticker, confidence=1.0)
