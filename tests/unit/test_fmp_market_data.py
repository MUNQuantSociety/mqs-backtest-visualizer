"""FMP prices, coverage and complete runs without a market-data database."""

import asyncio
import io
import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.contracts import NoMarketData, RunRequest
from engine.data import fmp
from engine.run_single import run_single
from src.api.routes import backtests, market_data
from src.schemas.market_data import CoverageResponse
from src.services import backtests as backtests_service
from src.services import market_data as coverage_service
from src.services.strategy_validation import runs as validation


@pytest.fixture(autouse=True)
def isolated_fmp(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp.time, "sleep", lambda _: None)
    coverage_service._fmp_span.cache_clear()
    coverage_service._symbol_cache.clear()
    yield
    coverage_service._fmp_span.cache_clear()
    coverage_service._symbol_cache.clear()


def bar(ticker="CRWV", day="2025-03-28", price=100):
    return {"symbol": ticker, "date": day, "open": str(price), "high": price + 1,
            "low": price - 1, "close": price, "volume": 1000}


def respond(monkeypatch, payload):
    calls = []

    def open_url(url, *, timeout):
        calls.append((parse_qs(urlparse(url).query), timeout))
        return io.StringIO(json.dumps(payload))

    monkeypatch.setattr(fmp, "urlopen", open_url)
    return calls


def test_source_selection_and_explicit_database_override(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "")
    assert fmp.market_data_source() == "fmp"
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    assert fmp.market_data_source() == "database"
    monkeypatch.setenv("MARKET_DATA_SOURCE", "")
    monkeypatch.setenv("FMP_API_KEY", "")
    assert fmp.market_data_source() == "fmp"
    with pytest.raises(fmp.FMPUnavailable, match="FMP_API_KEY is missing"):
        fmp.FMPMarketData()
    monkeypatch.setenv("MARKET_DATA_SOURCE", "typo")
    with pytest.raises(fmp.FMPUnavailable, match="MARKET_DATA_SOURCE"):
        fmp.market_data_source()


def test_daily_bars_are_inclusive_sorted_deduplicated_and_exchange_local(monkeypatch):
    calls = respond(monkeypatch, [bar(day="2025-03-10"), bar(day="2025-03-07"),
                                 bar(day="2025-03-10"), bar(day="2025-03-11")])
    frame = fmp.fetch_daily_history(["crwv", "CRWV"], "2025-03-07", "2025-03-10")
    assert len(calls) == 1
    assert calls[0][0] == {"symbol": ["CRWV"], "from": ["2025-03-07"],
                           "to": ["2025-03-10"], "apikey": ["test-secret-key"]}
    assert list(frame.timestamp.dt.strftime("%Y-%m-%d %H:%M %z")) == [
        "2025-03-07 16:00 -0500", "2025-03-10 16:00 -0400"]
    assert set(frame.columns) == {"ticker", "timestamp", "open_price", "high_price", "low_price", "close_price", "volume"}
    assert frame.open_price.tolist() == [100.0, 100.0]


@pytest.mark.parametrize("payload", [
    {"Error Message": "invalid key test-secret-key"}, None, ["bad"],
    [{**bar(), "close": None}], [{**bar(), "volume": -1}],
    [{**bar(), "open": "NaN"}], [{**bar(), "date": "invalid"}],
    [bar(ticker="WRONG")],
])
def test_bad_payload_is_a_provider_error_not_missing_data(monkeypatch, payload):
    respond(monkeypatch, payload)
    with pytest.raises(fmp.FMPUnavailable) as caught:
        fmp.fetch_daily_history(["CRWV"], "2025-03-28", "2025-03-31")
    assert "test-secret-key" not in str(caught.value)


@pytest.mark.parametrize("status,expected_attempts", [(401, 1), (402, 1), (403, 1), (429, 2), (503, 2)])
def test_http_errors_are_bounded_actionable_and_secret_safe(monkeypatch, caplog, status, expected_attempts):
    attempts = []

    def fail(url, **kwargs):
        attempts.append(url)
        raise HTTPError(url, status, "test-secret-key", {}, io.BytesIO(b"test-secret-key"))

    monkeypatch.setattr(fmp, "urlopen", fail)
    with pytest.raises(fmp.FMPUnavailable, match=f"HTTP {status}") as caught:
        fmp.fetch_daily_history(["CRWV"], "2025-03-28", "2025-03-31")
    assert len(attempts) == expected_attempts
    assert "test-secret-key" not in str(caught.value) + caplog.text


def test_an_unknown_symbol_answered_with_404_is_missing_history_not_an_outage(monkeypatch):
    def fail(url, **kwargs):
        raise HTTPError(url, 404, "test-secret-key", {}, io.BytesIO(b"test-secret-key"))

    monkeypatch.setattr(fmp, "urlopen", fail)
    with pytest.raises(fmp.FMPSymbolUnknown) as caught:
        fmp.fetch_daily_history(["NOPE"], "2025-03-28", "2025-03-31")
    assert "test-secret-key" not in str(caught.value)


def test_coverage_reports_a_404_symbol_as_missing_and_a_503_as_an_outage(monkeypatch):
    # validate-tickers recognizes symbols by lookup, never by history; the
    # 404-from-history distinction is coverage's alone.
    app = FastAPI()
    app.include_router(market_data.router)
    client = TestClient(app)

    def history(self, ticker, start, end):
        if ticker == "NOPE":
            raise fmp.FMPSymbolUnknown("FMP has no history for NOPE (HTTP 404).")
        if ticker == "DOWN":
            raise fmp.FMPUnavailable("FMP history for DOWN failed (HTTP 503).")
        return [{"date": date(2025, 3, 28)}]

    monkeypatch.setattr(fmp.FMPMarketData, "get_historical_data", history)

    response = client.get("/market-data/coverage", params={"tickers": "CRWV,NOPE"})
    assert response.status_code == 200
    assert response.json()["missing"] == ["NOPE"]
    assert response.json()["start"] is None

    response = client.get("/market-data/coverage", params={"tickers": "CRWV,DOWN"})
    assert response.status_code == 503


def test_timeout_does_not_look_like_missing_history(monkeypatch):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise URLError("test-secret-key")

    monkeypatch.setattr(fmp, "urlopen", fail)
    with pytest.raises(fmp.FMPUnavailable, match="could not be reached") as caught:
        fmp.fetch_daily_history(["CRWV"], "2025-03-28", "2025-03-31")
    assert len(attempts) == 2
    assert "test-secret-key" not in str(caught.value)


def test_empty_history_names_missing_symbol_and_allows_empty_warmup(monkeypatch):
    respond(monkeypatch, [])
    with pytest.raises(NoMarketData, match="CRWV"):
        fmp.fetch_daily_history(["CRWV"], "2025-03-28", "2025-03-31")
    assert fmp.fetch_daily_history(["CRWV"], "2025-03-01", "2025-03-27", require_all=False).empty


def forbidden(*args, **kwargs):
    pytest.fail("FMP must not read the database or the database parquet cache")


def test_coverage_uses_fmp_intersection_and_caches_form_lookups(monkeypatch):
    calls = []
    spans = {"CRWV": [date(2025, 3, 28), date(2026, 7, 15)],
             "NBIS": [date(2024, 10, 21), date(2026, 7, 15)]}

    def history(self, ticker, start, end):
        calls.append(ticker)
        return [{"date": day} for day in spans[ticker]]

    monkeypatch.setattr(fmp.FMPMarketData, "get_historical_data", history)
    monkeypatch.setattr(fmp.FMPMarketData, "symbol_exists", lambda self, ticker: True)
    monkeypatch.setattr(coverage_service, "session_scope", forbidden)
    response = asyncio.run(coverage_service.coverage_for(["crwv", "NBIS", "CRWV"]))
    assert response.start == "2025-03-28" and response.end == "2026-07-15"
    assert response.missing == []
    asyncio.run(coverage_service.coverage_for(["CRWV", "NBIS"]))
    assert sorted(calls) == ["CRWV", "NBIS"]
    asyncio.run(backtests_service._validated_coverage(["CRWV", "NBIS"], date(2025, 3, 28), date(2026, 7, 15)))
    with pytest.raises(backtests_service.RunSubmissionError, match="2025-03-28 to 2026-07-15"):
        asyncio.run(backtests_service._validated_coverage(["CRWV", "NBIS"], date(2024, 7, 15), date(2026, 7, 15)))


def test_coverage_missing_and_nonoverlapping_histories(monkeypatch):
    spans = {"CRWV": [date(2025, 3, 28)], "OLD": [date(2024, 1, 1)], "NOPE": []}
    monkeypatch.setattr(fmp.FMPMarketData, "get_historical_data",
                        lambda self, ticker, start, end: [{"date": day} for day in spans[ticker]])
    monkeypatch.setattr(fmp.FMPMarketData, "symbol_exists", lambda self, ticker: True)
    response = asyncio.run(coverage_service.coverage_for(["CRWV", "NOPE"]))
    assert response.missing == ["NOPE"] and response.start is None
    response = asyncio.run(coverage_service.coverage_for(["CRWV", "OLD"]))
    assert response.missing == [] and response.start is None and response.end is None
    with pytest.raises(backtests_service.RunSubmissionError, match="no shared market-data window"):
        asyncio.run(backtests_service._validated_coverage(["CRWV", "OLD"], date(2025, 3, 28), date(2025, 4, 1)))


def test_provider_failure_is_503_and_is_not_cached(monkeypatch):
    app = FastAPI()
    app.include_router(market_data.router)
    app.include_router(backtests.router)
    app.dependency_overrides[backtests.require_current_user] = (
        lambda: UUID("00000000-0000-0000-0000-000000000001")
    )

    def failure(*args, **kwargs):
        raise fmp.FMPUnavailable("FMP request limit reached. Retry shortly.")

    monkeypatch.setattr(fmp.FMPMarketData, "get_historical_data", failure)
    client = TestClient(app)
    response = client.get("/market-data/coverage", params={"tickers": "CRWV"})
    assert response.status_code == 503
    assert response.json() == {"detail": "FMP request limit reached. Retry shortly."}
    assert coverage_service._fmp_span.cache_info().currsize == 0

    async def fail_submission(request, *, owner_id):
        failure()

    monkeypatch.setattr(backtests_service, "submit_backtest_run", fail_submission)
    response = client.post("/backtests", json={
        "name": "FMP test", "strategyKey": "portfolio_1", "startDate": "2025-03-28",
        "endDate": "2025-04-04", "initialCapital": 10000, "mode": "event", "params": {},
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "FMP request limit reached. Retry shortly."}


def test_upload_validation_uses_fmp_and_stays_inside_new_ticker_history(monkeypatch):
    async def coverage(tickers):
        return CoverageResponse(tickers=[], start="2025-03-28", end="2025-04-04")

    monkeypatch.setattr(validation, "coverage_for", coverage)
    monkeypatch.setattr(validation, "session_scope", forbidden)
    assert asyncio.run(validation.validation_window(["CRWV"])) == (date(2025, 3, 28), date(2025, 4, 4))


@pytest.mark.parametrize("mode,class_path", [
    ("event", "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy"),
    ("fast", "engine.strategies.portfolio_1.strategy:VolMomentum"),
])
def test_complete_run_uses_fmp_for_warmup_prices_and_benchmark(monkeypatch, tmp_path, mode, class_path):
    calls = []

    def history(url, *, timeout):
        query = parse_qs(urlparse(url).query)
        calls.append(query)
        ticker = query["symbol"][0]
        days = pd.bdate_range(query["from"][0], query["to"][0])
        rows = [bar(ticker, day.date().isoformat(), 100 + day.dayofyear / 10) for day in days
                if day >= pd.Timestamp("2025-03-28")]
        return io.StringIO(json.dumps(rows[::-1]))

    monkeypatch.setattr(fmp, "urlopen", history)
    monkeypatch.setattr("engine.run_single.EngineDBAdapter", forbidden)
    monkeypatch.setattr("engine.data.cache.load", forbidden)
    monkeypatch.setattr("engine.data.cache.save", forbidden)
    result = run_single(RunRequest(
        run_id="fmp-test", strategy_key="fmp-test", class_path=class_path,
        start_date="2025-03-28", end_date="2025-04-04", initial_capital=10000,
        mode=mode, params={"TICKERS": ["CRWV", "NBIS"], "WEIGHTS": {"CRWV": 0.5, "NBIS": 0.5}, "LOOKBACK_DAYS": 5},
        artifact_dir=str(tmp_path),
    ))
    assert result.status == "completed", result.error
    assert result.equity_curve[-1].date == date(2025, 4, 4)
    assert result.report_metadata["marketData"] == {"source": "fmp", "resolution": "daily"}
    assert result.report_metadata["benchmark"]["universe"] == ["CRWV", "NBIS"]
    assert result.report_metadata["execution"]["fillCount"] == len(result.fills)
    if mode == "fast":
        assert "does not mean the strategy made no trades" in result.report_metadata["execution"]["message"]
        assert "strategyDiagnostics" not in result.report_metadata
    assert {query["symbol"][0] for query in calls} == {"CRWV", "NBIS"}
    assert len(calls) == 2  # warmup, simulation and benchmark share one download per ticker
    assert any(query["from"][0] < "2025-03-28" for query in calls)
    assert all(query["to"][0] == "2025-04-04" for query in calls)


def test_run_cache_remembers_empty_pre_ipo_days_without_reusing_other_runs(monkeypatch):
    calls = respond(monkeypatch, [bar()])
    adapter = fmp.FMPDataAdapter()
    main = adapter.get_daily_history(["CRWV"], "2025-01-01", "2025-03-28")
    assert len(main) == 1
    assert adapter.get_daily_history(["CRWV"], "2025-01-01", "2025-03-27", require_all=False).empty
    assert len(adapter.get_daily_history(["CRWV"], "2025-03-28", "2025-03-28")) == 1
    assert len(calls) == 1
    with pytest.raises(NoMarketData, match="CRWV"):
        adapter.get_daily_history(["CRWV"], "2025-01-01", "2025-03-27")
    with pytest.raises(fmp.FMPUnavailable, match="Database price queries are disabled"):
        adapter.execute_query("SELECT * FROM market_data")
    adapter.close()
    fmp.FMPDataAdapter().get_daily_history(["CRWV"], "2025-01-01", "2025-03-28")
    assert len(calls) == 2


def test_actual_vol_momentum_fills_orders_with_ready_bullish_daily_history(monkeypatch, tmp_path):
    days = pd.bdate_range("2025-01-01", "2025-04-04")
    calls = respond(monkeypatch, [bar("AAPL", day.date().isoformat(), 100 + i) for i, day in enumerate(days)])
    monkeypatch.setattr("engine.run_single.EngineDBAdapter", forbidden)
    monkeypatch.setattr("engine.data.cache.load", forbidden)
    result = run_single(RunRequest(
        run_id="vol-momentum-fmp", strategy_key="portfolio_1",
        class_path="engine.strategies.portfolio_1.strategy:VolMomentum",
        start_date="2025-03-28", end_date="2025-04-04", initial_capital=10000,
        mode="event", params={"TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1.0}, "LOOKBACK_DAYS": 90},
        artifact_dir=str(tmp_path),
    ))
    assert result.status == "completed", result.error
    assert result.fills and result.fills[0]["signal_type"] == "BUY"
    assert result.final_equity != 10000
    assert result.report_metadata["execution"] == {"fillCount": len(result.fills), "message": None}
    diagnostics = result.report_metadata["strategyDiagnostics"]
    assert diagnostics["evaluationCount"] == 6
    assert diagnostics["bullishSignalCount"] == 6
    assert diagnostics["bearishSignalCount"] == 0
    assert diagnostics["buyRequestCount"] > 0
    assert diagnostics["tickers"]["AAPL"]["strongestMomentum"]["momentumToThresholdRatio"] > 1
    assert len(calls) == 1


@pytest.mark.parametrize("warmup,explanation", [
    (False, "enough ready market and indicator history"),
    (True, "No ticker exceeded the strategy's bullish entry threshold"),
])
def test_actual_vol_momentum_explains_zero_fills_without_changing_the_rule(monkeypatch, tmp_path, warmup, explanation):
    days = pd.bdate_range("2025-01-01" if warmup else "2025-03-28", "2025-04-04")
    respond(monkeypatch, [bar("AAPL", day.date().isoformat(), 100) for day in days])
    result = run_single(RunRequest(
        run_id="vol-momentum-no-fills", strategy_key="portfolio_1",
        class_path="engine.strategies.portfolio_1.strategy:VolMomentum",
        start_date="2025-03-28", end_date="2025-04-04", initial_capital=10000,
        mode="event", params={"TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1.0}, "LOOKBACK_DAYS": 90},
        artifact_dir=str(tmp_path),
    ))
    assert result.status == "completed", result.error
    assert result.fills == []
    assert result.final_equity == 10000
    assert result.report_metadata["execution"]["fillCount"] == 0
    assert explanation in result.report_metadata["execution"]["message"]
    diagnostics = result.report_metadata["strategyDiagnostics"]
    assert diagnostics["bullishSignalCount"] == diagnostics["buyRequestCount"] == 0
    assert diagnostics["evaluationCount"] == (6 if warmup else 0)
    assert diagnostics["warmupSkipCount"] == (0 if warmup else 6)
    if warmup:
        assert diagnostics["tickers"]["AAPL"]["strongestMomentum"] == {
            "date": "2025-03-28", "momentumPct": 0.0,
            "thresholdPct": 0.0, "momentumToThresholdRatio": None,
        }
    json.dumps(result.report_metadata, allow_nan=False)
