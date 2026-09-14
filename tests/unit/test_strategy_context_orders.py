"""Strategy helper execution proofs using real sizing, settlement, and FIFO pairing."""

from types import SimpleNamespace

import pandas as pd
import pytest

from engine.core.executor import BacktestExecutor
from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio
from src.services.strategy_validation.template import STARTER_SOURCE
from src.services.trade_pairing import pair_fills


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Order helper tests must not contact a database")
    monkeypatch.setattr("psycopg2.connect", forbidden)


class ImmediateOMS:
    """Exercise the real fixed-child settlement seam after parent sizing."""

    def __init__(self, executor):
        self.executor = executor
        self.orders = []

    def process_order(self, **order):
        self.orders.append(order)
        child = SimpleNamespace(
            ticker=order["ticker"], signal_type=SimpleNamespace(value=order["side"]),
            target_quantity=order["total_quantity"], confidence=order["confidence"],
            portfolio_id=order["portfolio_id"], child_id="regression",
        )
        result = self.executor.execute_child_order(child, order["timestamp"])
        assert result["status"] == "success", result


def context(executor, *, oms=False, weights=None):
    feeds = executor.get_data_feeds()
    moment = pd.Timestamp("2026-03-02 16:00", tz="America/New_York")
    prices = pd.DataFrame([
        {"timestamp": moment, "ticker": ticker, "close_price": executor.latest_prices[ticker]}
        for ticker in executor.tickers
    ])
    return StrategyContext(
        prices, feeds["CASH_EQUITY"], feeds["POSITIONS"], feeds["PORT_NOTIONAL"],
        moment, executor,
        {"id": "order-regression", "tickers": executor.tickers, "weights": weights},
        order_manager=ImmediateOMS(executor) if oms else None,
    )


def executor(*, position=0, cash=2000, price=100, leverage=2, fee=0.005, slippage=0.0005):
    result = BacktestExecutor(cash + position * price, ["AAPL", "MSFT"],
                              leverage=leverage, commission_per_share=fee, slippage=slippage)
    result.cash = cash
    result.positions["AAPL"] = position
    result.update_price("AAPL", price)
    result.update_price("MSFT", price)
    return result


@pytest.mark.parametrize("oms", [False, True])
def test_actual_starter_source_buys_then_closes_without_opening_short(oms):
    namespace = {}
    exec(compile(STARTER_SOURCE, "starter_strategy.py", "exec"), namespace)
    strategy = namespace["MyStrategy"].__new__(namespace["MyStrategy"])
    strategy.tickers = ["AAPL"]
    # Indicators are stubbed, but the starter's declared STATE is built the way
    # __init__ would, so this setup does not have to track what it declares.
    BasePortfolio._init_declared_state(strategy)
    strategy.fast_sma = {"AAPL": SimpleNamespace(IsReady=True, Current=110)}
    strategy.slow_sma = {"AAPL": SimpleNamespace(IsReady=True, Current=100)}
    broker = executor()
    strategy.OnData(context(broker, oms=oms, weights={"AAPL": 0.25}))
    assert broker.positions["AAPL"] == 4
    strategy.fast_sma["AAPL"].Current = 90
    strategy.OnData(context(broker, oms=oms, weights={"AAPL": 0.25}))
    assert [fill["signal_type"] for fill in broker.trade_log] == ["BUY", "SELL"]
    assert [fill["shares"] for fill in broker.trade_log] == [4, 4]
    assert broker.positions["AAPL"] == 0
    closed, = pair_fills(broker.trade_log)
    assert closed.side == "long" and closed.exit_date is not None
    assert broker.cash - 2000 == pytest.approx(closed.pnl - closed.fees)


def raw_execute_trade(broker, ticker, side, confidence, ticker_weight, ctx):
    """What portfolio_3 used to call by reaching into the context's internals."""
    asset = ctx.Market[ticker]
    broker.execute_trade(
        portfolio_id=ctx._portfolio_config["id"], ticker=ticker, signal_type=side,
        confidence=confidence, arrival_price=asset.Close, cash=ctx.Portfolio.cash,
        positions=ctx._positions_df, port_notional=ctx.Portfolio.total_value,
        ticker_weight=ticker_weight, timestamp=ctx.time,
    )


@pytest.mark.parametrize("weight", [
    1.0 / 12,  # the flat-position fallback share
    0.083,     # a configured weight, the shape a held position passes
    0.4,
])
@pytest.mark.parametrize("side,position", [("BUY", 0), ("BUY", 5), ("SELL", 12)])
def test_execute_fills_exactly_as_the_direct_executor_call_did(side, position, weight):
    through_context = executor(position=position)
    ctx = context(through_context)
    ctx.execute("AAPL", side, 0.65, ticker_weight=weight)

    directly = executor(position=position)
    raw_execute_trade(directly, "AAPL", side, 0.65, weight, context(directly))

    assert through_context.positions == directly.positions
    assert through_context.cash == pytest.approx(directly.cash)
    assert [fill["shares"] for fill in through_context.trade_log] == [
        fill["shares"] for fill in directly.trade_log
    ]


@pytest.mark.parametrize("side,position", [("BUY", 0), ("SELL", 12)])
def test_execute_hands_the_sized_parent_to_a_configured_order_manager(side, position):
    # Same seam as buy/sell: with an OMS the executor only sizes, and the OMS
    # owns execution. Without one, execute() settles directly.
    broker = executor(position=position)
    ctx = context(broker, oms=True)
    ctx.execute("AAPL", side, 0.65, ticker_weight=0.4)

    (order,) = ctx._order_manager.orders
    assert order["ticker"] == "AAPL" and order["side"] == side
    assert order["total_quantity"] > 0
    directly = executor(position=position)
    raw_execute_trade(directly, "AAPL", side, 0.65, 0.4, context(directly))
    assert broker.positions == directly.positions


def test_execute_requires_the_weight_rather_than_guessing_a_universe():
    with pytest.raises(TypeError):
        context(executor()).execute("AAPL", "BUY", 1.0)


def test_execute_does_not_trade_a_ticker_with_no_market_data():
    broker = executor()
    feeds = broker.get_data_feeds()
    no_prices = pd.DataFrame(columns=["timestamp", "ticker", "close_price"])
    ctx = StrategyContext(
        no_prices, feeds["CASH_EQUITY"], feeds["POSITIONS"], feeds["PORT_NOTIONAL"],
        pd.Timestamp("2026-03-02 16:00", tz="America/New_York"), broker,
        {"id": "order-regression", "tickers": broker.tickers, "weights": None},
    )
    ctx.execute("AAPL", "BUY", 1.0, ticker_weight=0.25)

    assert broker.trade_log == []


@pytest.mark.parametrize("oms", [False, True])
@pytest.mark.parametrize("position,confidence,expected", [
    (11, 1, 0), (11, 0.5, 6), (11, 0.1, 10), (11, 0, 11),
    (11, 2, 0), (11, -1, 11), (10.5, 1, 0.5), (0, 1, 0), (-11, 1, -11),
])
def test_sell_only_reduces_whole_long_shares_without_reversing(oms, position, confidence, expected):
    broker = executor(position=position)
    ctx = context(broker, oms=oms)
    ctx.sell("AAPL", confidence=confidence)
    assert broker.positions["AAPL"] == expected
    if position > 0:
        assert 0 <= broker.positions["AAPL"] <= position
    else:
        assert broker.trade_log == []
    # Even repeated calls through the same snapshot cannot open a short.
    ctx.sell("AAPL", confidence=confidence)
    if position >= 0:
        assert broker.positions["AAPL"] >= 0


@pytest.mark.parametrize("oms", [False, True])
@pytest.mark.parametrize("fee,slippage", [(0, 0), (0.005, 0.0005)])
def test_zero_free_margin_does_not_prevent_a_long_exit(oms, fee, slippage):
    broker = executor(position=20, cash=0, leverage=1, fee=fee, slippage=slippage)
    assert broker._calculate_buying_power(broker.get_port_notional()) == 0
    context(broker, oms=oms).sell("AAPL")
    assert broker.positions["AAPL"] == 0
    assert broker.trade_log[0]["shares"] == 20
    assert broker.cash == pytest.approx(20 * (100 * (1 - slippage) - fee))


@pytest.mark.parametrize("oms", [False, True])
def test_buy_covers_short_and_reaches_configured_positive_allocation(oms):
    broker = executor(position=-20, cash=4000, leverage=1, fee=0, slippage=0)
    assert broker._calculate_buying_power(broker.get_port_notional()) == 0
    context(broker, oms=oms, weights={"AAPL": 0.25}).buy("AAPL")
    assert broker.positions["AAPL"] == 5
    assert broker.trade_log[0]["signal_type"] == "BUY"
    assert broker.trade_log[0]["shares"] == 25
    assert broker.cash == 1500


@pytest.mark.parametrize("oms", [False, True])
@pytest.mark.parametrize("weights,expected", [
    (None, 10), ({"AAPL": 0.25}, 5), ([0.25, 0.75], 5),
    ({"AAPL": 0}, 0), ({"MSFT": 1}, 0), ({}, 0),
])
def test_buy_uses_existing_config_allocations_and_preserves_explicit_zero(oms, weights, expected):
    broker = executor(fee=0, slippage=0)
    context(broker, oms=oms, weights=weights).buy("AAPL")
    assert broker.positions["AAPL"] == expected


@pytest.mark.parametrize("oms", [False, True])
def test_explicit_negative_target_supports_intentional_short_and_close(oms):
    broker = executor(fee=0, slippage=0)
    context(broker, oms=oms).target_weight("AAPL", -0.25)
    assert broker.positions["AAPL"] == -5
    context(broker, oms=oms).sell("AAPL")
    assert len(broker.trade_log) == 1
    context(broker, oms=oms).close_position("AAPL")
    assert broker.positions["AAPL"] == 0
    closed, = pair_fills(broker.trade_log)
    assert closed.side == "short" and closed.exit_date is not None


@pytest.mark.parametrize("oms", [False, True])
def test_explicit_target_keeps_fee_cash_and_margin_limits_for_new_exposure(oms):
    broker = executor(cash=100, price=10, fee=1, slippage=0)
    context(broker, oms=oms).target_weight("AAPL", 1)
    assert broker.positions["AAPL"] == 9 and broker.cash == 1


@pytest.mark.parametrize("oms", [False, True])
def test_full_exit_retains_all_whole_shares_even_below_dollar_minimum(oms):
    broker = executor(position=1, price=0.5, fee=0, slippage=0)
    context(broker, oms=oms).sell("AAPL")
    assert broker.positions["AAPL"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_explicit_target_is_rejected_before_settlement(bad):
    broker = executor()
    with pytest.raises(ValueError, match="finite"):
        context(broker).target_weight("AAPL", bad)
    assert broker.trade_log == []


def test_low_level_legacy_sell_still_accepts_an_intentional_short_target():
    broker = executor(fee=0, slippage=0)
    broker.execute_trade(
        portfolio_id="legacy", ticker="AAPL", signal_type="SELL", confidence=1,
        arrival_price=100, cash=broker.cash, positions=broker.positions,
        port_notional=broker.get_port_notional(), ticker_weight=0.25, timestamp=None,
    )
    assert broker.positions["AAPL"] == -5
