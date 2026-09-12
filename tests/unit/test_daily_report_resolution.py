"""Daily FMP reports must never allocate or serialize a synthetic minute grid."""

import io
import json
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.analytics import reporting
from engine.contracts import RunRequest
from engine.core.executor import BacktestExecutor
from engine.data import fmp
from engine.run_single import run_single


def _forbidden(*args, **kwargs):
    pytest.fail("Daily FMP reporting must not expand to minute data or access SQL")


def test_actual_fmp_run_skips_minute_allocation_and_preserves_observed_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr("engine.run_single.EngineDBAdapter", _forbidden)
    monkeypatch.setattr("engine.data.cache.load", _forbidden)
    monkeypatch.setattr(reporting, "_generate_minute_by_minute_performance", _forbidden)
    rows = [
        {"symbol": "AAPL", "date": day, "open": price, "high": price + 1,
         "low": price - 1, "close": price, "volume": 1000}
        for day, price in [("2025-01-13", 100), ("2025-01-14", 110)]
    ]
    monkeypatch.setattr(fmp, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(rows)))
    result = run_single(RunRequest(
        run_id="daily-export", strategy_key="portfolio_dummy",
        class_path="engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
        start_date="2025-01-13", end_date="2025-01-14", initial_capital=10000,
        mode="event", params={"TICKERS": ["AAPL"], "LOOKBACK_DAYS": 30,
                              "WEIGHTS": {"AAPL": 1.0}}, artifact_dir=str(tmp_path),
    ))
    assert result.status == "completed", result.error
    assert result.final_equity == 10000
    assert [point.benchmark for point in result.equity_curve] == pytest.approx([10000, 10000, 11000])
    observed = pd.read_csv(tmp_path / "performance_timeseries_absolute.csv")
    benchmark = pd.read_csv(tmp_path / "benchmark_buy_and_hold.csv")
    assert len(observed) == len(benchmark) == 2
    assert observed.portfolio_value.tolist() == [10000, 10000]
    assert benchmark.buy_and_hold_value.tolist() == pytest.approx([10000, 11000])
    assert not (tmp_path / "performance_timeseries_minute_by_minute.csv").exists()


def test_disabling_minute_export_preserves_all_other_frames_and_legacy_default(tmp_path):
    timestamps = pd.date_range("2025-01-13 09:30", periods=3, freq="2min", tz="America/New_York")
    prices = pd.DataFrame({"timestamp": timestamps, "ticker": "AAPL", "close_price": [100, 110, 120]})
    perf = pd.DataFrame({"timestamp": timestamps, "portfolio_value": 1000.0})
    portfolio = SimpleNamespace(
        executor=BacktestExecutor(initial_capital=1000, tickers=["AAPL"]),
        tickers=["AAPL"], portfolio_weights={"AAPL": 1.0}, portfolio_id="resolution",
        logger=logging.getLogger("test_daily_report_resolution"),
    )
    legacy = reporting.generate_backtest_report(
        portfolio, perf, 1000, prices, out_dir=str(tmp_path / "legacy"),
    )
    daily = reporting.generate_backtest_report(
        portfolio, perf, 1000, prices, out_dir=str(tmp_path / "daily"), include_minute_report=False,
    )
    minute_key = "performance_timeseries_minute_by_minute"
    assert len(legacy[minute_key]) == 5  # Existing intraday default still expands 2-minute bars.
    assert legacy[minute_key].portfolio_value.tolist() == [1000] * 5
    assert legacy.keys() - daily.keys() == {minute_key}
    assert daily.keys() - legacy.keys() == set()
    for key, frame in daily.items():
        pd.testing.assert_frame_equal(frame, legacy[key])
    assert len(daily["performance_timeseries_absolute"]) == 3
    assert daily["benchmark_buy_and_hold"].buy_and_hold_value.tolist() == pytest.approx([1000, 1100, 1200])
