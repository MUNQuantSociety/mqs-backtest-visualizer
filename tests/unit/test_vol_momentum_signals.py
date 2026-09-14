"""Real strategy decisions with daily history, including the all-cash regression."""

import copy
import json
import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from engine.run_single import _execution_summary
from engine.strategies.portfolio_1.strategy import VolMomentum


def decide(
    momentum,
    *,
    position=0,
    observations=42,
    ready=True,
    exists=True,
    cash=100000,
    return_diagnostics=False,
):
    # About 60 calendar days contains 42 daily bars, never 61 bars. Alternating
    # 1% daily moves are ~1.03% daily volatility; over the strategy's 20-bar
    # signal horizon that is ~4.6%, so the 1.5x threshold sits near 6.9%.
    prices = 100 * np.cumprod(1 + np.resize([0.01, -0.01], observations))
    history = pd.DataFrame({"close_price": prices})
    calls = []
    strategy = VolMomentum.__new__(VolMomentum)
    strategy.logger = logging.getLogger("vol-momentum-regression")
    # The same deep copy BasePortfolio._init_declared_state makes, so a test
    # that bypasses __init__ still starts from fresh counters.
    strategy.strategy_diagnostics = copy.deepcopy(
        VolMomentum.STATE["strategy_diagnostics"]
    )
    strategy.tickers = ["AAPL"]
    strategy.roc = {"AAPL": SimpleNamespace(IsReady=ready, Current=momentum)}
    context = SimpleNamespace(
        time=pd.Timestamp("2025-04-04 16:00", tz="America/New_York"),
        Market={
            "AAPL": SimpleNamespace(
                Exists=exists, Close=prices[-1], History=lambda window: history
            )
        },
        Portfolio=SimpleNamespace(
            cash=cash,
            total_value=100000,
            positions={"AAPL": position},
            get_asset_weight=lambda ticker, price: 0.1 if position else 0,
        ),
        buy=lambda ticker, **kw: calls.append(("buy", ticker)),
        sell=lambda ticker, **kw: calls.append(("sell", ticker)),
    )
    strategy.OnData(context)
    return (calls, strategy.strategy_diagnostics) if return_diagnostics else calls


def test_daily_window_can_generate_a_buy_instead_of_nan_and_no_trades():
    assert decide(50) == [("buy", "AAPL")]


def test_momentum_and_volatility_are_compared_in_the_same_percent_units():
    assert decide(1) == []


def test_bearish_signal_closes_an_existing_position():
    assert decide(-50, position=10) == [("sell", "AAPL")]


def test_risk_off_blocks_a_new_long():
    # Cash under 10% of the book: no new exposure, whatever momentum says.
    assert decide(50, cash=5000) == []


def test_risk_off_keeps_a_bullish_position_rather_than_flattening_it():
    # A fully invested five-name book is always "risk-off" by the cash test.
    # Selling its still-bullish holdings for that reason alone liquidated and
    # re-bought the whole book on alternate bars, paying costs both ways.
    assert decide(50, position=10, cash=5000) == []


def test_risk_off_still_lets_a_bearish_position_close():
    assert decide(-50, position=10, cash=5000) == [("sell", "AAPL")]


def test_recent_listing_waits_for_enough_daily_returns():
    assert decide(50, observations=20) == []


@pytest.mark.parametrize("momentum", [None, float("nan"), float("inf")])
def test_invalid_ready_indicator_fails_instead_of_silently_holding_cash(momentum):
    with pytest.raises(ValueError, match="non-finite momentum or volatility"):
        decide(momentum)


@pytest.mark.parametrize(
    "momentum,position,bullish,bearish,buy,sell",
    [
        (50, 0, 1, 0, 1, 0),
        (1, 0, 0, 0, 0, 0),
        (-50, 10, 0, 1, 0, 1),
        (-50, 0, 0, 1, 0, 0),
    ],
)
def test_diagnostics_distinguish_thresholds_from_trade_requests(
    momentum, position, bullish, bearish, buy, sell
):
    _, diagnostics = decide(momentum, position=position, return_diagnostics=True)
    assert diagnostics["evaluationCount"] == 1
    assert diagnostics["warmupSkipCount"] == 0
    assert diagnostics["bullishSignalCount"] == bullish
    assert diagnostics["bearishSignalCount"] == bearish
    assert diagnostics["buyRequestCount"] == buy
    assert diagnostics["sellRequestCount"] == sell
    ticker = diagnostics["tickers"]["AAPL"]
    assert ticker["evaluationCount"] == 1
    snapshot = ticker["strongestMomentum"]
    assert snapshot["date"] == "2025-04-04"
    assert snapshot["momentumPct"] == momentum
    assert 6 < snapshot["thresholdPct"] < 8
    assert snapshot["momentumToThresholdRatio"] == pytest.approx(
        momentum / snapshot["thresholdPct"]
    )
    json.dumps(diagnostics, allow_nan=False)


@pytest.mark.parametrize(
    "kwargs,key",
    [
        ({"observations": 20}, "warmupSkipCount"),
        ({"ready": False}, "warmupSkipCount"),
        ({"exists": False}, "missingMarketDataSkipCount"),
    ],
)
def test_diagnostics_identify_unevaluated_observations(kwargs, key):
    calls, diagnostics = decide(50, return_diagnostics=True, **kwargs)
    assert calls == []
    assert diagnostics["evaluationCount"] == 0
    assert diagnostics[key] == 1
    assert diagnostics["tickers"]["AAPL"]["strongestMomentum"] is None


@pytest.mark.parametrize(
    "momentum,kwargs,explanation",
    [
        (50, {"observations": 20}, "enough ready market and indicator history"),
        (1, {}, "No ticker exceeded the strategy's bullish entry threshold"),
        (50, {}, "requested trades, but none produced a fill"),
    ],
)
def test_zero_fill_summary_explains_warmup_signals_and_execution(
    momentum, kwargs, explanation
):
    _, diagnostics = decide(momentum, return_diagnostics=True, **kwargs)
    summary = _execution_summary("event", [], diagnostics)
    assert summary["fillCount"] == 0
    assert explanation in summary["message"]
    assert (
        "Trade metrics that require executed or closed trades are unavailable."
        in summary["message"]
    )


def test_fast_summary_does_not_infer_no_trades_from_an_empty_fill_table():
    summary = _execution_summary("fast", [], None)
    assert summary["fillCount"] == 0
    assert "does not mean the strategy made no trades" in summary["message"]
    assert _execution_summary("event", [{"ticker": "AAPL"}], None) == {
        "fillCount": 1,
        "message": None,
    }
