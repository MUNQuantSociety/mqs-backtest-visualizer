"""Bounded report proofs using observed fixtures and real engine execution, no DB."""

import importlib
import json
import logging
import pickle
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.analytics.reporting import (
    _generate_buy_and_hold_benchmark,
    benchmark_weights,
    market_timestamps,
)
from engine.contracts import EngineError, RunRequest
from engine.core.backtest_engine import BacktestEngine
from engine.core.runner import BacktestRunner
from engine.strategies.portfolio_BASE.strategy import BasePortfolio

single = importlib.import_module("engine.run_single")


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    def forbidden(*args, **kwargs):
        pytest.fail("Report unit tests must never connect to PostgreSQL")
    monkeypatch.setattr("psycopg2.connect", forbidden)


def bars(rows):
    frame = pd.DataFrame(rows, columns=["timestamp", "ticker", "close_price"])
    frame["timestamp"] = market_timestamps(frame["timestamp"])
    return frame


@pytest.mark.parametrize("raw, expected", [
    (None, {"A": 0.5, "B": 0.5}),
    ([0.6, 0.2], {"A": 0.6, "B": 0.2}),
    ({"A": 0.0, "B": 0.0}, {"A": 0.0, "B": 0.0}),
    ({}, {"A": 0.0, "B": 0.0}),
    ({"A": 0.6, "OUTSIDE": 0.4}, {"A": 0.6, "B": 0.0}),
])
def test_configured_weights_preserve_cash_and_universe(raw, expected):
    assert benchmark_weights(raw, ["A", "B"]) == expected


@pytest.mark.parametrize("raw", [[0.5], "equal", {"A": -0.1}, {"A": float("inf")}, {"A": float("nan")}, {"A": None}])
def test_invalid_weights_are_not_silently_replaced(raw):
    with pytest.raises(ValueError, match="Benchmark WEIGHTS"):
        benchmark_weights(raw, ["A", "B"])


def test_same_window_weighted_hold_preserves_cash_and_first_intraday_close():
    prices = bars([
        ("2026-01-01 16:00", "A", 1),
        ("2026-01-02 09:30", "A", 100),
        ("2026-01-02 09:30", "B", 200),
        ("2026-01-02 16:00", "A", 120),
        ("2026-01-02 16:00", "B", 100),
        ("2026-01-05 16:00", "A", 150),
        ("2026-01-05 16:00", "B", 160),
        ("2026-01-06 16:00", "A", 99999),
    ])
    result = _generate_buy_and_hold_benchmark(
        prices, 1000, {"A": 0.6, "B": 0.2},
        start="2026-01-02", end="2026-01-05 21:00Z",
    )
    assert result.buy_and_hold_value.tolist() == pytest.approx([1000, 1020, 1260])
    assert result.buy_and_hold_return.tolist() == pytest.approx([0, 0.02, 0.26])
    assert len(result) == 3  # no weekend or calendar-minute rows
    assert result.attrs["report_metadata"]["cash_weight"] == pytest.approx(0.2)
    assert result.attrs["report_metadata"]["coverage"] == "complete"


def test_delayed_and_missing_tickers_reserve_cash_without_future_prices():
    prices = bars([
        ("2026-01-01 16:00", "B", 1),  # warmup must not supply an entry
        ("2026-01-02 10:00", "A", 100),
        ("2026-01-02 11:00", "A", 110),
        ("2026-01-02 12:00", "B", 200),
        ("2026-01-02 13:00", "B", 220),
    ])
    weights = {"A": 0.5, "B": 0.3, "MISSING": 0.1}
    result = _generate_buy_and_hold_benchmark(prices, 1000, weights, start="2026-01-02")
    assert result.buy_and_hold_value.tolist() == pytest.approx([1000, 1050, 1050, 1080])
    prefix = _generate_buy_and_hold_benchmark(prices.iloc[:3], 1000, weights, start="2026-01-02")
    assert prefix.buy_and_hold_value.tolist() == pytest.approx(result.buy_and_hold_value.iloc[:2])
    metadata = result.attrs["report_metadata"]
    assert metadata["missing_tickers"] == ["MISSING"]
    assert metadata["delayed_tickers"] == ["B"]
    assert metadata["coverage"] == "partial"


def test_sparse_long_window_never_allocates_grid(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Benchmark must not allocate a calendar grid or price matrix")
    monkeypatch.setattr(pd.DataFrame, "resample", forbidden)
    monkeypatch.setattr(pd.DataFrame, "pivot", forbidden)
    prices = bars([("2000-01-03 16:00", "A", 100), ("2050-01-03 16:00", "A", 120)])
    result = _generate_buy_and_hold_benchmark(prices, 1000, {"A": 1})
    assert len(result) == 2
    assert result.buy_and_hold_value.tolist() == pytest.approx([1000, 1200])


def test_invalid_and_duplicate_quotes_do_not_poison_benchmark():
    prices = bars([
        ("2026-01-02 10:00", "A", 99),
        ("2026-01-02 10:00", "A", 100),
        ("2026-01-02 11:00", "A", 0),
        ("2026-01-02 12:00", "A", float("inf")),
        ("2026-01-02 13:00", "A", 120),
    ])
    result = _generate_buy_and_hold_benchmark(prices, 1000, {"A": 1})
    assert result.buy_and_hold_value.tolist() == pytest.approx([1000, 1200])
    with pytest.raises(ValueError):
        _generate_buy_and_hold_benchmark(prices, 1000, {"A": 1}, start="not a date")


def test_explicit_all_cash_and_unavailable_prices():
    prices = bars([("2026-01-02", "A", 100), ("2026-01-05", "A", 200)])
    assert _generate_buy_and_hold_benchmark(prices, 1000, {"A": 0}).buy_and_hold_value.tolist() == [1000, 1000]
    assert _generate_buy_and_hold_benchmark(prices, 1000, {"B": 1}).empty


def test_curve_uses_asof_marks_and_explicit_baseline_without_altering_performance():
    prices = bars([("2026-01-02 10:00", "A", 100), ("2026-01-02 16:00", "A", 120)])
    benchmark = _generate_buy_and_hold_benchmark(prices, 1000, {"A": 1})
    perf = pd.DataFrame({"timestamp": market_timestamps(pd.Series([
        "2026-01-02 09:00", "2026-01-02 12:00", "2026-01-02 16:00"
    ])), "portfolio_value": [1000, 990, 1100]})
    original = perf.copy(deep=True)
    curve = single._equity_curve(perf, benchmark, 1000)
    assert [point.benchmark for point in curve] == [None, None, 1000, 1200]
    assert [point.equity for point in curve] == [1000, 1000, 990, 1100]
    pd.testing.assert_frame_equal(perf, original)


def test_new_york_dates_include_naive_labels_utc_and_dst_offsets():
    moments = pd.Series(["2026-03-06", "2026-03-07T01:00:00Z", "2026-03-06T16:00:00-05:00", "2026-03-09T16:00:00-04:00"])
    assert single._ny_dates(moments).tolist() == [date(2026, 3, 6)] * 3 + [date(2026, 3, 9)]


def test_invalid_final_equity_is_a_failure():
    with pytest.raises(EngineError, match="invalid performance"):
        single._equity_curve(pd.DataFrame({"timestamp": ["2026-01-02"], "portfolio_value": [float("inf")]}))


def request(tmp_path, mode="event", class_path=None, **overrides):
    kwargs = dict(
        run_id="benchmark-test", strategy_key="fixture",
        class_path=class_path or "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
        start_date="2026-01-02", end_date="2026-01-06", initial_capital=1000,
        mode=mode, artifact_dir=str(tmp_path),
        params={"TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1.0}, "LOOKBACK_DAYS": 0, "INTERVAL": 3600},
    )
    kwargs.update(overrides)
    return RunRequest(**kwargs)


def test_real_event_runner_and_entrypoint_keep_final_bar_marks(monkeypatch, tmp_path):
    from engine.strategies.portfolio_dummy.strategy import CrossoverRmiStrategy
    prices = bars([
        ("2026-01-02 10:00", "AAPL", 100),
        ("2026-01-02 10:01", "AAPL", 100.01),
        ("2026-01-02 10:02", "AAPL", 100.02),
    ])
    monkeypatch.setattr("engine.core.runner.fetch_historical_data", lambda *args: prices.copy())
    # Only replace indicator construction and signal policy. Configuration,
    # executor settlement, runner/report generation, and run_single are real.
    monkeypatch.setattr(CrossoverRmiStrategy, "__init__", BasePortfolio.__init__)
    polls = []
    def buy_once(self, data, current_time):
        polls.append(current_time)
        self.executor.execute_trade(
            portfolio_id=self.portfolio_id, signal_type="BUY", ticker="AAPL",
            confidence=1.0, arrival_price=100, cash=1000, positions={},
            port_notional=1000, ticker_weight=0.5, timestamp=current_time,
        )
    monkeypatch.setattr(CrossoverRmiStrategy, "generate_signals_and_trade", buy_once)
    monkeypatch.setattr(single, "EngineDBAdapter", lambda: SimpleNamespace(close=lambda: None))
    result = single.run_single(request(tmp_path, slippage=0.01))
    assert result.status == "completed", result.error
    assert len(polls) == 1  # final sampling never causes an extra strategy poll
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert result.final_prices == {"AAPL": 100.02}
    assert result.final_equity == pytest.approx(fill["cash_after"] + fill["shares"] * 100.02)
    assert result.equity_curve[-1].benchmark == pytest.approx(1000.2)
    assert result.equity_curve[0].equity == 1000
    assert result.equity_curve[1].equity < 1000  # slippage retained after baseline
    assert result.equity_curve[-1].equity == result.final_equity
    assert result.report_metadata["equity"]["baseline"]["curve_index"] == 0
    assert result.report_metadata["benchmark"]["universe"] == ["AAPL"]
    assert pickle.loads(pickle.dumps(result)).report_metadata == result.report_metadata
    json.dumps(result.report_metadata, allow_nan=False)


def test_final_sample_includes_oms_fill_after_last_poll():
    calls = []
    portfolio = SimpleNamespace(
        logger=logging.getLogger("fixture"), tickers=["AAPL"], poll_interval=3600,
        lookback_days=0, generate_signals_and_trade=lambda *args, **kwargs: calls.append(1),
    )
    runner = BacktestRunner(portfolio, "2026-01-02", "2026-01-03", initial_capital=1000)
    runner.main_data_df = bars([("2026-01-02 10:00", "AAPL", 100), ("2026-01-02 10:01", "AAPL", 120)])
    def pump(now, execute_child):
        if now.minute == 1:
            execute_child(SimpleNamespace(
                ticker="AAPL", signal_type=SimpleNamespace(value="BUY"),
                target_quantity=2, confidence=1, portfolio_id="fixture",
            ))
    runner.order_manager = SimpleNamespace(manage_order=pump)
    runner._setup_executor()
    runner._run_event_loop()
    assert calls == [1]
    assert len(runner.executor.trade_log) == 1
    assert runner.perf_records[-1]["AAPL"] == 240
    assert runner.perf_records[-1]["portfolio_value"] == 1000
    assert runner.final_prices == {"AAPL": 120}


@pytest.mark.parametrize("class_path", [
    "engine.strategies.portfolio_1.strategy:VolMomentum",
    "engine.strategies.portfolio_2.strategy:MomentumStrategy",
    "engine.strategies.portfolio_3.strategy:RegimeAdaptiveStrategy",
])
def test_real_supported_fast_paths_use_configured_hold(monkeypatch, tmp_path, class_path):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    days = pd.bdate_range("2025-09-01", "2026-01-05")
    rows = [
        {"timestamp": day.tz_localize("America/New_York") + pd.Timedelta(hours=16),
         "ticker": ticker, "close_price": float(price)}
        for i, day in enumerate(days)
        for ticker, price in [("AAPL", 100 + i), ("MSFT", 300 - i)]
    ]
    class MemoryAdapter:
        closed = False
        def execute_query(self, *args, **kwargs):
            return {"status": "success", "data": rows}
        def close(self):
            self.closed = True
    adapter = MemoryAdapter()
    monkeypatch.setattr(single, "EngineDBAdapter", lambda: adapter)
    result = single.run_single(request(
        tmp_path, "fast", class_path=class_path,
        params={"TICKERS": ["AAPL", "MSFT"], "WEIGHTS": [0.75, 0.15]},
    ))
    assert result.status == "completed", result.error
    assert adapter.closed
    assert result.fills == [] and result.final_prices == {}
    assert [point.date for point in result.equity_curve] == [date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 5)]
    first_i, last_i = days.get_loc("2026-01-02"), days.get_loc("2026-01-05")
    expected = 1000 * (0.1 + 0.75 * (100 + last_i) / (100 + first_i) + 0.15 * (300 - last_i) / (300 - first_i))
    assert result.equity_curve[1].benchmark == pytest.approx(1000)
    assert result.equity_curve[-1].benchmark == pytest.approx(expected)
    assert result.final_equity == result.equity_curve[-1].equity
    metadata = result.report_metadata["benchmark"]
    assert metadata["weights"] == {"AAPL": 0.75, "MSFT": 0.15}
    assert metadata["price_sampling"] == "daily_close"
    assert metadata["first_observation"].startswith("2026-01-02T16:00")
    assert (tmp_path / "benchmark_buy_and_hold.csv").exists()
    # Raw vector performance (including its first-day return) is preserved.
    perf_path = next(tmp_path.rglob("performance_timeseries_absolute.csv"))
    original_perf = pd.read_csv(perf_path)
    assert result.final_equity == pytest.approx(original_perf.portfolio_value.iloc[-1])
    json.dumps(result.report_metadata, allow_nan=False)


@pytest.mark.parametrize("mode, phrase", [("fast", "Fast mode is not available"), ("typo", "Unsupported backtest mode")])
def test_unsupported_paths_fail_before_database_construction(monkeypatch, tmp_path, mode, phrase):
    def forbidden():
        pytest.fail("Unsupported mode must fail before constructing the DB adapter")
    monkeypatch.setattr(single, "EngineDBAdapter", forbidden)
    result = single.run_single(request(tmp_path, mode))
    assert result.status == "failed" and phrase in result.error
    assert result.equity_curve == []


def test_fast_output_cannot_invent_a_bar():
    engine = BacktestEngine(None)
    engine.last_fast_perf_df = pd.DataFrame({"timestamp": ["2026-01-05"], "portfolio_value": [1000]})
    engine.fast_benchmark_prices = bars([("2026-01-02 16:00", "AAPL", 100)])
    with pytest.raises(EngineError, match="without an observed market bar"):
        single._fast_mode_perf(engine)
