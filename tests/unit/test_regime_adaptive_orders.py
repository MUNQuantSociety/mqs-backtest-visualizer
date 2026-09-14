"""portfolio_3's sell signals close a long; they never open a short.

The strategy's own guard (``if signal == "SELL" and quantity <= 0: continue``)
says as much, but the order it then placed was ``execute(..., "SELL",
ticker_weight=w)``, and under the executor's legacy signal model a SELL
targets *minus* that weight — a long went straight through flat into a short.
"""

import copy
import logging
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

from engine.strategies.portfolio_3.strategy import RegimeAdaptiveStrategy


def decide(*, position: float, price: float, entry_price: float | None = None):
    """Drive one OnData bar for AAPL in a calm, low-volatility regime."""
    calls = []
    strategy = RegimeAdaptiveStrategy.__new__(RegimeAdaptiveStrategy)
    strategy.logger = logging.getLogger("regime-adaptive-orders")
    strategy.tickers = ["AAPL", "^VIX"]
    strategy.poll_interval = 60
    strategy.portfolio_weights = {"AAPL": 0.5}
    for attr, default in RegimeAdaptiveStrategy.STATE.items():
        setattr(strategy, attr, copy.deepcopy(default))
    if entry_price is not None:
        strategy.entry_price["AAPL"] = entry_price

    ready = lambda value: SimpleNamespace(IsReady=True, Current=value)  # noqa: E731
    strategy.vwap = {"AAPL": ready(100.0)}
    strategy.atr = {"AAPL": ready(1.0)}
    strategy.sma50 = {"AAPL": ready(100.0)}
    # Momentum well under -MOMENTUM_THRESHOLD: a bearish signal in the
    # low-volatility (momentum) regime.
    strategy.momentum_pct = {"AAPL": ready(-5.0)}
    strategy.vix_ema = ready(15.0)

    context = SimpleNamespace(
        time=pd.Timestamp("2025-04-04 16:00", tz="America/New_York"),
        Market={
            "AAPL": SimpleNamespace(Exists=True, Close=price),
            "^VIX": SimpleNamespace(Exists=True, Close=14.0),
        },
        Portfolio=SimpleNamespace(
            positions={"AAPL": position},
            get_asset_weight=lambda ticker, px: (position * px) / 100_000,
        ),
        execute=lambda ticker, side, confidence, **kw: calls.append(("execute", side, ticker, kw)),
        sell=lambda ticker, confidence=1.0: calls.append(("sell", ticker)),
        buy=lambda ticker, confidence=1.0: calls.append(("buy", ticker)),
    )
    strategy.OnData(context)
    return calls


def test_a_bearish_signal_on_a_long_closes_it_rather_than_shorting():
    calls = decide(position=100, price=99.0, entry_price=100.0)

    assert calls == [("sell", "AAPL")]


def test_a_bearish_signal_with_no_position_places_nothing():
    assert decide(position=0, price=99.0) == []


def test_a_stop_loss_exit_also_closes_rather_than_shorting():
    # Entry 110, ATR 1, STOP_LOSS_ATR_MULT 3: 99 is well past the stop.
    calls = decide(position=100, price=99.0, entry_price=110.0)

    assert calls == [("sell", "AAPL")]
