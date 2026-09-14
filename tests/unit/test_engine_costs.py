"""Explicit execution-cost proofs. Market data and DB boundaries are mocked."""

import importlib
import pickle
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.analytics.reporting import _generate_minute_by_minute_performance
from engine.contracts import RunRequest
from engine.core.backtest_engine import BacktestEngine
from engine.core.cost_model import CostModel, CostModelParams
from engine.core.executor import BacktestExecutor
from engine.data import fmp
from engine.strategies.portfolio_BASE.strategy import BasePortfolio
from src.services.trade_pairing import pair_fills

single = importlib.import_module("engine.run_single")


@pytest.fixture(autouse=True)
def no_external_data(monkeypatch):
    # Local .env credentials must not turn a leaking unit test into a pass.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("FMP_API_KEY", "")
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")

    def forbidden(*args, **kwargs):
        pytest.fail("Cost unit tests must never access external FMP or database data")
    monkeypatch.setattr("psycopg2.connect", forbidden)
    monkeypatch.setattr(fmp.FMPMarketData, "__init__", forbidden)
    monkeypatch.setattr(fmp, "urlopen", forbidden)


def trade(executor, side="BUY", weight=1.0, moment="2026-03-02 10:00"):
    timestamp = pd.Timestamp(moment)
    timestamp = (timestamp.tz_localize("America/New_York") if timestamp.tzinfo is None
                 else timestamp.tz_convert("America/New_York"))
    return executor.execute_trade(
        portfolio_id="cost-test", ticker="AAPL", signal_type=side, confidence=1,
        arrival_price=executor.latest_prices["AAPL"], cash=executor.cash,
        positions=executor.positions, port_notional=executor.get_port_notional(),
        ticker_weight=weight, timestamp=timestamp,
    )


def child(executor, side, quantity, price, minute=0):
    executor.update_price("AAPL", price)
    order = SimpleNamespace(
        ticker="AAPL", signal_type=SimpleNamespace(value=side),
        target_quantity=quantity, confidence=1, portfolio_id="cost-test",
        child_id="fixture",
    )
    return executor.execute_child_order(
        order, pd.Timestamp("2026-03-02 10:00", tz="America/New_York") + pd.Timedelta(minutes=minute)
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_zero_commission_keeps_legacy_fill_amounts(side):
    implicit = BacktestExecutor(1000, ["AAPL"], slippage=0.0005)
    explicit = BacktestExecutor(1000, ["AAPL"], slippage=0.0005, commission_per_share=0)
    for executor in (implicit, explicit):
        executor.update_price("AAPL", 100)
        trade(executor, side=side, weight=0.5)
    assert implicit.cash == explicit.cash
    assert implicit.positions == explicit.positions
    assert implicit.trade_log == explicit.trade_log
    fill = explicit.trade_log[0]
    expected_price = 100 * (1.0005 if side == "BUY" else 0.9995)
    assert fill["fill_price"] == pytest.approx(expected_price)
    assert fill["shares"] == int(500 // expected_price)
    assert fill["fees"] == 0


def test_buy_reserves_cash_for_commission_without_changing_target():
    executor = BacktestExecutor(100, ["AAPL"], commission_per_share=1)
    executor.update_price("AAPL", 10)
    result = trade(executor)
    # Ten shares cost 110 including fees; nine shares cost 99.
    assert result["quantity"] == 9
    assert result["fees"] == 9
    assert executor.cash == 1
    assert executor.get_port_notional() == 91
    assert executor.trade_log[0]["fees"] == 9


def test_commission_does_not_reduce_target_when_cash_and_margin_are_sufficient():
    executor = BacktestExecutor(1000, ["AAPL"], commission_per_share=1)
    executor.update_price("AAPL", 10)
    result = trade(executor, weight=0.1)
    assert result["quantity"] == 10
    assert executor.cash == 890


def test_short_sale_reserves_margin_for_commission():
    executor = BacktestExecutor(10000, ["AAPL"], commission_per_share=1)
    executor.update_price("AAPL", 10)
    trade(executor, side="SELL", weight=2)
    # q*10 <= 2*(10000-q*1): at most 1666 whole shares.
    assert executor.positions["AAPL"] == -1666
    assert executor.cash == 10000 + 1666 * 10 - 1666
    assert abs(executor.get_position_value("AAPL")) <= 2 * executor.get_port_notional()
    assert executor.trade_log[0]["fees"] == 1666


def test_no_fill_when_one_share_plus_fee_is_unaffordable():
    executor = BacktestExecutor(10, ["AAPL"], commission_per_share=1)
    executor.update_price("AAPL", 10)
    assert trade(executor) is None
    assert executor.cash == 10 and executor.trade_log == []


def test_child_rechecks_affordability_without_partial_settlement():
    executor = BacktestExecutor(100, ["AAPL"], commission_per_share=1)
    result = child(executor, "BUY", 10, 10)
    assert result["status"] == "error"
    assert "including commission" in result["message"]
    assert executor.cash == 100 and executor.positions["AAPL"] == 0
    assert executor.trade_log == []
    result = child(executor, "BUY", 9, 10)
    assert result["filled_quantity"] == 9 and result["fees"] == 9
    assert executor.cash == 1


def test_cash_fees_reconcile_partial_realized_and_open_unrealized_pnl():
    executor = BacktestExecutor(1000, ["AAPL"], commission_per_share=1)
    assert child(executor, "BUY", 10, 10)["status"] == "success"
    assert child(executor, "SELL", 4, 12, minute=1)["status"] == "success"
    closed, opened = pair_fills(executor.trade_log)
    assert closed.pnl == 8 and closed.fees == 8
    assert opened.pnl == 0 and opened.fees == 6
    unrealized = (12 - opened.entry_price) * opened.quantity
    assert executor.get_port_notional() == 1006
    assert executor.get_port_notional() - 1000 == closed.pnl - closed.fees + unrealized - opened.fees
    assert sum(fill["fees"] for fill in executor.trade_log) == closed.fees + opened.fees


def test_short_round_trip_pnl_includes_both_cash_commissions():
    executor = BacktestExecutor(1000, ["AAPL"], commission_per_share=0.005, slippage=0.0005)
    assert child(executor, "SELL", 5, 100)["status"] == "success"
    assert child(executor, "BUY", 5, 90, minute=1)["status"] == "success"
    closed, = pair_fills(executor.trade_log)
    assert closed.side == "short"
    assert closed.pnl == pytest.approx((100 * 0.9995 - 90 * 1.0005) * 5)
    assert closed.fees == pytest.approx(10 * 0.005)
    assert executor.positions["AAPL"] == 0
    assert executor.cash - 1000 == pytest.approx(closed.pnl - closed.fees)


@pytest.mark.parametrize("position, cash, signal, settled", [(10, 1000, "BUY", "SELL"), (-10, 3000, "SELL", "BUY")])
def test_target_reduction_uses_actual_fill_side_for_slippage_and_log(position, cash, signal, settled):
    executor = BacktestExecutor(2000, ["AAPL"], commission_per_share=0.005, slippage=0.0005)
    executor.positions["AAPL"] = position
    executor.cash = cash
    executor.update_price("AAPL", 100)
    result = trade(executor, side=signal, weight=0.25)
    fill = executor.trade_log[0]
    assert fill["signal_type"] == settled
    assert fill["fill_price"] == pytest.approx(100 * (1.0005 if settled == "BUY" else 0.9995))
    signed_notional = fill["shares"] * fill["fill_price"] * (1 if settled == "BUY" else -1)
    assert result["updated_cash"] == pytest.approx(cash - signed_notional - fill["fees"])


def test_legacy_cost_model_is_still_available_to_direct_engine_callers():
    model = CostModel(CostModelParams(fixed_bps=100, enable_spread=False, enable_impact=False))
    executor = BacktestExecutor(1000, ["AAPL"], slippage=0.03, cost_model=model)
    executor.update_price("AAPL", 100)
    trade(executor)
    assert executor.trade_log[0]["fill_price"] == pytest.approx(101)


def run_request(tmp_path, **kwargs):
    values = dict(
        run_id="cost-test", strategy_key="fixture",
        class_path="engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
        start_date="2026-03-02", end_date="2026-03-03", initial_capital=1000,
        artifact_dir=str(tmp_path),
        params={"TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1}, "LOOKBACK_DAYS": 0, "INTERVAL": 3600},
    )
    values.update(kwargs)
    return RunRequest(**values)


def test_browser_default_costs_reach_real_event_execution_and_artifacts(monkeypatch, tmp_path):
    from engine.strategies.portfolio_dummy.strategy import CrossoverRmiStrategy
    prices = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-03-02 10:00", "2026-03-02 10:01"]).tz_localize("America/New_York"),
        "ticker": ["AAPL", "AAPL"], "close_price": [100.0, 100.0],
    })
    history_requests = []

    def fixture_history(tickers, start, end, *, require_all=True):
        assert tickers == ["AAPL"]
        history_requests.append((start, end))
        days = prices.timestamp.dt.date
        return prices.loc[days.between(pd.Timestamp(start).date(), pd.Timestamp(end).date())].copy()

    # Stub only the provider boundary: keep the real adapter's prefetch/cache
    # and the runner's history lookup. A runner-only stub misses the prefetch.
    monkeypatch.setattr(fmp, "fetch_daily_history", fixture_history)
    # BasePortfolio.__init__ builds whatever INDICATORS declares, so emptying
    # the declaration is now part of "no indicators", not the base call alone.
    monkeypatch.setattr(CrossoverRmiStrategy, "INDICATORS", {})
    monkeypatch.setattr(CrossoverRmiStrategy, "__init__", BasePortfolio.__init__)
    monkeypatch.setattr(CrossoverRmiStrategy, "generate_signals_and_trade", lambda self, data, current_time: trade(self.executor, moment=current_time))
    def forbidden_model(*args, **kwargs):
        pytest.fail("RunRequest price slippage must not be replaced by a legacy CostModel")
    monkeypatch.setattr(CostModel, "apply_to_price", forbidden_model)
    result = single.run_single(run_request(tmp_path, slippage=5 / 10000, commission_per_share=0.005))
    assert result.status == "completed", result.error
    assert len(history_requests) == 1, "The runner must reuse the adapter's prefetched fixture history"
    assert result.report_metadata["marketData"] == {"source": "fmp", "resolution": "daily"}
    fill, = result.fills
    assert fill["shares"] == 9
    assert fill["fill_price"] == pytest.approx(100.05)
    assert fill["fees"] == pytest.approx(0.045)
    assert fill["cash_after"] == pytest.approx(99.505)
    assert result.final_equity == pytest.approx(999.505)
    assert result.final_prices == {"AAPL": 100}
    assert result.equity_curve[0].equity == 1000
    assert result.equity_curve[-1].benchmark == 1000  # benchmark remains cost-free
    assert result.report_metadata["executionCosts"]["commissionPerShare"] == 0.005
    assert result.report_metadata["executionCosts"]["slippageFraction"] == 0.0005
    assert result.report_metadata["executionCosts"]["legacyCostModel"] is False
    assert pickle.loads(pickle.dumps(result)).fills == result.fills
    stored_fills = pd.read_csv(tmp_path / "trade_log.csv")
    assert stored_fills.fees.iloc[0] == pytest.approx(fill["fees"])
    minute = _generate_minute_by_minute_performance(result.fills, prices, 1000, ["AAPL"])
    assert minute.portfolio_value.iloc[-1] == pytest.approx(result.final_equity)


@pytest.mark.parametrize("costs, message", [
    ({"commission_per_share": -1}, "commission_per_share"),
    ({"commission_per_share": float("inf")}, "commission_per_share"),
    ({"commission_per_share": float("nan")}, "commission_per_share"),
    ({"slippage": -0.1}, "slippage"),
    ({"slippage": 1}, "slippage"),
    ({"slippage": float("nan")}, "slippage"),
    ({"mode": "fast", "commission_per_share": 0.005}, "Fast mode does not support per-share commission"),
])
def test_bad_or_unsupported_costs_fail_before_adapter_construction(monkeypatch, tmp_path, costs, message):
    def forbidden():
        pytest.fail("Cost rejection must precede market-data adapter construction")
    monkeypatch.setattr(single, "EngineDBAdapter", forbidden)
    monkeypatch.setattr(single, "FMPDataAdapter", forbidden)
    result = single.run_single(run_request(tmp_path, **costs))
    assert result.status == "failed" and message in result.error
    assert result.fills == [] and result.equity_curve == []


def test_direct_fast_setup_also_rejects_per_share_costs():
    with pytest.raises(ValueError, match="Fast mode does not support per-share commission"):
        BacktestEngine(None).setup([], "2026-03-02", "2026-03-03", 1000, backtest_mode="fast", commission_per_share=0.005)
