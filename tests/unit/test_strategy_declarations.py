"""What a strategy declares, and what BasePortfolio does with it.

A strategy is meant to be a config file, a few class attributes and OnData.
These cover the two halves of that promise: every key config.json can carry
reaches the instance, and the declarations build the indicators and state that
a strategy would otherwise write an __init__ for.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.strategies.portfolio_BASE.strategy import BasePortfolio

CONFIG = {
    "PORTFOLIO_ID": "7",
    "TICKERS": ["AAPL", "MSFT"],
    "INTERVAL": 900,
    "LOOKBACK_DAYS": 45,
    "WEIGHTS": {"AAPL": 0.6, "MSFT": 0.4},
    "DATA_FEEDS": ["MARKET_DATA"],
    "EXCH": "NASDAQ",
    "OMS": {"enabled": True, "default_algo": "TWAP"},
}


@pytest.fixture
def db():
    """A connector that answers the warmup query with no rows."""
    return SimpleNamespace(
        execute_query=lambda *args, **kwargs: {"status": "success", "data": []}
    )


@pytest.fixture(autouse=True)
def local_market_data(monkeypatch):
    """Pin the warmup to the query path, so no test here reaches FMP."""
    monkeypatch.setattr(
        "engine.strategies.portfolio_BASE.strategy.market_data_source",
        lambda: "database",
    )


def build(cls, db, config=None):
    return cls(db_connector=db, executor=None, config_dict=config or dict(CONFIG))


class Plain(BasePortfolio):
    def OnData(self, context):
        pass


class Declared(BasePortfolio):
    INDICATORS = {
        "sma": ("SimpleMovingAverage", {"period": 5}),
        "vix_ema": ("ExponentialMovingAverage", {"period": 10}, "MSFT"),
    }
    STATE = {"seen": {}, "history": []}
    PER_TICKER_STATE = {"marks": {"fast": 0.0}}

    def OnData(self, context):
        pass


class Imperative(BasePortfolio):
    """The pre-declaration shape every uploaded strategy still uses."""

    def __init__(self, db_connector, executor, debug=False, config_dict=None,
                 backtest_start_date=None, order_manager=None):
        super().__init__(db_connector, executor, debug, config_dict,
                         backtest_start_date, order_manager)
        self.RegisterIndicatorSet({"sma": ("SimpleMovingAverage", {"period": 5})})

    def OnData(self, context):
        pass


# --- Config -----------------------------------------------------------------


def test_every_config_key_reaches_the_instance(db):
    strategy = build(Plain, db)

    assert strategy.portfolio_id == "7"
    assert strategy.tickers == ["AAPL", "MSFT"]
    assert strategy.poll_interval == 900
    assert strategy.lookback_days == 45
    assert strategy.portfolio_weights == {"AAPL": 0.6, "MSFT": 0.4}
    assert strategy.data_feeds == ["MARKET_DATA"]
    assert strategy.exchange == "NASDAQ"
    assert strategy.oms_config == {"enabled": True, "default_algo": "TWAP"}


def test_config_keys_no_attribute_is_named_for_are_still_readable(db):
    config = dict(CONFIG) | {"RBP_CONFIG": {"cap": 3}}
    assert build(Plain, db, config).config["RBP_CONFIG"] == {"cap": 3}


def test_initial_capital_is_overridable_per_portfolio(db):
    assert build(Plain, db).initial_capital == BasePortfolio.DEFAULT_INITIAL_CAPITAL
    override = build(Plain, db, dict(CONFIG) | {"INITIAL_CAPITAL": 250_000})
    assert override.initial_capital == 250_000


def test_a_missing_config_falls_back_to_documented_defaults(db):
    strategy = Plain(db_connector=db, executor=None, config_dict=None)

    assert strategy.portfolio_id == "0"
    assert strategy.tickers == []
    assert strategy.exchange is None
    assert strategy.oms_config is None


def test_the_context_sees_the_config_the_instance_sees(db):
    strategy = build(Plain, db)

    assert strategy.portfolio_config_dict["exchange"] == "NASDAQ"
    assert strategy.portfolio_config_dict["oms"] == CONFIG["OMS"]
    assert strategy.portfolio_config_dict["config"]["PORTFOLIO_ID"] == "7"


# --- Declared indicators ----------------------------------------------------


def test_declared_indicators_are_built_for_every_ticker(db):
    strategy = build(Declared, db)

    assert sorted(strategy.sma) == ["AAPL", "MSFT"]
    assert strategy.sma["AAPL"].ticker == "AAPL"


def test_a_declared_single_ticker_indicator_is_the_indicator_itself(db):
    strategy = build(Declared, db)

    assert strategy.vix_ema.ticker == "MSFT"
    assert not isinstance(strategy.vix_ema, dict)


def test_a_declared_indicator_the_engine_lacks_is_refused(db):
    class Missing(Plain):
        INDICATORS = {"nope": ("NoSuchIndicator", {"period": 5})}

    with pytest.raises(ImportError):
        build(Missing, db)


def test_registering_indicators_by_hand_still_works(db):
    assert sorted(build(Imperative, db).sma) == ["AAPL", "MSFT"]


# --- Declared state ---------------------------------------------------------


def test_declared_state_is_assigned_from_its_default(db):
    strategy = build(Declared, db)

    assert strategy.seen == {}
    assert strategy.history == []
    assert strategy.marks == {"AAPL": {"fast": 0.0}, "MSFT": {"fast": 0.0}}


def test_two_runs_of_one_strategy_never_share_mutable_state(db):
    first, second = build(Declared, db), build(Declared, db)

    first.seen["AAPL"] = 1
    first.marks["AAPL"]["fast"] = 99.0

    assert second.seen == {}
    assert second.marks["AAPL"] == {"fast": 0.0}
    assert Declared.STATE["seen"] == {}


# --- The indicator update path a strategy relies on -------------------------


def bar(**fields):
    row = {
        "timestamp": pd.Timestamp("2026-01-05 16:00", tz="UTC"),
        "ticker": "AAPL",
        "close_price": 101.0,
        "high_price": 103.0,
        "low_price": 99.0,
        "volume": 2_500_000.0,
    }
    row.update(fields)
    return next(pd.DataFrame([row]).itertuples())


def test_volume_reaches_a_volume_weighted_indicator(db):
    strategy = build(Plain, db)
    vwap = SimpleNamespace(
        ticker="AAPL", price_col="close_price", vol_col="volume", seen=[],
    )
    vwap.Update = lambda ts, price, **kw: vwap.seen.append((price, kw))

    assert strategy._update_indicator_from_row(vwap, bar()) is True
    assert vwap.seen == [(101.0, {"volume": 2_500_000.0})]


def test_the_daily_range_reaches_a_true_range_indicator(db):
    strategy = build(Plain, db)
    atr = SimpleNamespace(
        ticker="AAPL", high_col="high_price", low_col="low_price",
        close_col="close_price", seen=[],
    )
    atr.Update = lambda ts, price, **kw: atr.seen.append((price, kw))

    strategy._update_indicator_from_row(atr, bar())
    price, kwargs = atr.seen[0]

    assert price == 101.0
    assert kwargs["high_price"] == 103.0
    assert kwargs["low_price"] == 99.0


def test_a_bar_with_no_price_updates_nothing(db):
    strategy = build(Plain, db)
    indicator = SimpleNamespace(ticker="AAPL", price_col="close_price", seen=[])
    indicator.Update = lambda ts, price, **kw: indicator.seen.append(price)

    assert strategy._update_indicator_from_row(indicator, bar(close_price=np.nan)) is False
    assert indicator.seen == []
