"""FMP cache boundaries must not change the strategy's observable history."""

import io
import json
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from engine.contracts import RunRequest
from engine.core.runner import BacktestRunner
from engine.data import fmp
from engine.run_single import run_single
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


def _bar(day, price, ticker="AAPL"):
    return {
        "symbol": ticker, "date": day, "open": price, "high": price + 1,
        "low": price - 1, "close": price, "volume": 1000,
    }


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def forbidden(*args, **kwargs):
        pytest.fail("FMP runs must not access live providers, SQL, or DB cache")

    monkeypatch.setattr(fmp, "urlopen", forbidden)
    monkeypatch.setattr("engine.run_single.EngineDBAdapter", forbidden)
    monkeypatch.setattr("engine.data.cache.load", forbidden)
    monkeypatch.setattr("engine.data.cache.save", forbidden)


def _respond(monkeypatch, rows):
    calls = []

    def open_url(url, *, timeout):
        calls.append(parse_qs(urlparse(url).query))
        # Deliberately return bars outside the requested window as well.
        return io.StringIO(json.dumps(rows))

    monkeypatch.setattr(fmp, "urlopen", open_url)
    return calls


class ObserveHistory(BasePortfolio):
    def __init__(self, adapter):
        super().__init__(
            adapter, executor=None, backtest_start_date=pd.Timestamp("2025-01-13"),
            config_dict={"PORTFOLIO_ID": "isolation", "TICKERS": ["AAPL"],
                         "LOOKBACK_DAYS": 30, "WEIGHTS": {"AAPL": 1.0}},
        )
        self.sma = self.AddIndicator("SimpleMovingAverage", ticker="AAPL", period=2)
        self.observations = []

    def OnData(self, context):
        asset = context.Market["AAPL"]
        history = asset.History("30 days")
        self.observations.append((context.time, history, asset.Close, self.sma.Current))


def test_prefetched_future_prices_never_enter_warmup_or_earlier_event(monkeypatch):
    calls = _respond(monkeypatch, [
        _bar("2025-01-09", 100), _bar("2025-01-10", 200),
        _bar("2025-01-13", 300), _bar("2025-01-14", 1_000_000),
        _bar("2025-01-15", 2_000_000),
    ])
    adapter = fmp.FMPDataAdapter()
    adapter.get_daily_history(["AAPL"], "2024-10-01", "2025-01-14")
    portfolio = ObserveHistory(adapter)
    assert portfolio.sma.Current == 150  # Only Thursday and Friday warm up Monday.

    runner = BacktestRunner(portfolio, "2025-01-13", "2025-01-14", strict=True)
    assert runner._prepare_data()
    runner._setup_executor()
    runner._run_event_loop()

    assert len(calls) == 1
    assert len(portfolio.observations) == 2
    first_time, first_history, first_close, first_sma = portfolio.observations[0]
    assert first_close == 300
    assert first_sma == 250
    assert first_history.close_price.tolist() == [100, 200, 300]
    assert first_history.index.max() == first_time
    for current_time, history, _, _ in portfolio.observations:
        assert (history.index <= current_time).all()
    assert runner.final_prices == {"AAPL": 1_000_000}


def test_returned_frames_and_new_runs_cannot_mutate_or_reuse_another_cache(monkeypatch):
    rows = [_bar("2025-01-13", 100)]
    calls = _respond(monkeypatch, rows)
    first_adapter = fmp.FMPDataAdapter()
    frame = first_adapter.get_daily_history(["AAPL"], "2025-01-13", "2025-01-13")
    frame.loc[:, "close_price"] = 999
    frame["trade_date"] = frame.timestamp.dt.date
    cached = first_adapter.get_daily_history(["AAPL"], "2025-01-13", "2025-01-13")
    assert cached.close_price.tolist() == [100]
    assert "trade_date" not in cached

    rows[:] = [_bar("2025-01-13", 200)]
    second_adapter = fmp.FMPDataAdapter()
    fresh = second_adapter.get_daily_history(["AAPL"], "2025-01-13", "2025-01-13")
    assert fresh.close_price.tolist() == [200]
    assert len(calls) == 2
    first_adapter.close()
    assert second_adapter.get_daily_history(
        ["AAPL"], "2025-01-13", "2025-01-13"
    ).close_price.tolist() == [200]
    assert len(calls) == 2


@pytest.mark.parametrize("rows", [[], [_bar("2025-01-10", 100)]])
def test_no_simulation_history_fails_instead_of_successful_flat_fallback(monkeypatch, tmp_path, rows):
    calls = _respond(monkeypatch, rows)
    result = run_single(RunRequest(
        run_id="empty-fmp", strategy_key="portfolio_dummy",
        class_path="engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
        start_date="2025-01-13", end_date="2025-01-14", initial_capital=10000,
        mode="event", params={"TICKERS": ["AAPL"], "LOOKBACK_DAYS": 30,
                              "WEIGHTS": {"AAPL": 1.0}},
        artifact_dir=str(tmp_path),
    ))
    assert result.status == "failed"
    # NoMarketData, in its own words: the empty window and what to check.
    assert "Check the ticker coverage" in result.error
    assert not result.error.startswith("NoMarketData")
    assert result.equity_curve == []
    assert result.final_equity is None
    assert len(calls) == 1
