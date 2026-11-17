"""FMP indicator lines for the Build chart, and the list of indicators it offers.

The series must be the numbers a backtest's ``FmpIndicator`` reads, so the
endpoint uses the indicator's own warmed-up loader rather than a raw request.
"""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.data.fmp import TECHNICAL_INDICATORS, FMPUnavailable
from engine.indicators import fmp_indicator
from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_data as market_data_api
from src.services import market_data


@pytest.fixture
def fmp(monkeypatch):
    """Records indicator requests and answers with the values a test sets."""
    state = {"values": {}, "calls": [], "error": None, "exists": True, "lookups": []}

    class FakeProvider:
        def get_technical_indicator(self, ticker, name, period, start, end):
            state["calls"].append((ticker, name, period, start, end))
            if state["error"]:
                raise state["error"]
            return sorted((day, value) for day, value in state["values"].items() if start <= day <= end)

    def symbol_exists(ticker):
        state["lookups"].append(ticker)
        return state["exists"]

    monkeypatch.setattr(market_data, "FMPMarketData", FakeProvider)
    monkeypatch.setattr(market_data, "_fmp_symbol_exists", symbol_exists)
    market_data._series_cache.clear()
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def series(ticker="aapl", indicator="RSI", period=14, start=date(2026, 9, 1), end=date(2026, 9, 30)):
    return asyncio.run(market_data.indicator_series_between(ticker, indicator, period, start, end))


def test_returns_points_oldest_first_for_the_window(fmp):
    fmp["values"] = {date(2026, 8, 31): 10.0, date(2026, 9, 2): 55.5, date(2026, 9, 1): 50.0}

    result = series()

    assert result.ticker == "AAPL" and result.indicator == "rsi" and result.period == 14
    assert [(p.date, p.value) for p in result.points] == [("2026-09-01", 50.0), ("2026-09-02", 55.5)]


def test_requests_start_with_the_backtest_indicators_warmup(fmp):
    fmp["values"] = {date(2026, 9, 1): 50.0}

    series()

    ticker, name, period, start, end = fmp["calls"][0]
    assert (ticker, name, period, end) == ("AAPL", "rsi", 14, date(2026, 9, 30))
    assert start == date(2026, 9, 1) - timedelta(days=fmp_indicator.warmup_days(14))


def test_a_repeat_request_is_served_from_the_cache(fmp):
    fmp["values"] = {date(2026, 9, 1): 50.0}

    series()
    series()

    assert len(fmp["calls"]) == 1


@pytest.mark.parametrize("kwargs,message", [
    ({"indicator": "macd"}, "Unknown indicator"),
    ({"period": 1}, "period"),
    ({"period": 251}, "period"),
    ({"start": date(2026, 9, 30), "end": date(2026, 9, 1)}, "start"),
    ({"start": date(2000, 1, 1), "end": date(2026, 1, 1)}, "15 years"),
    ({"ticker": "not a ticker!"}, "ticker"),
])
def test_bad_requests_are_refused_before_any_fmp_call(fmp, kwargs, message):
    with pytest.raises(ValueError, match=message):
        series(**kwargs)
    assert fmp["calls"] == []


def test_an_unknown_symbol_is_said_plainly(fmp, client):
    fmp["exists"] = False

    response = client.get("/api/market-data/indicator-series",
                          params={"ticker": "ZZZZ", "indicator": "rsi", "period": 14,
                                  "start": "2026-09-01", "end": "2026-09-30"})

    assert response.status_code == 404
    assert "ZZZZ" in response.json()["detail"]


def test_a_real_symbol_with_no_values_is_an_empty_line(fmp, client):
    response = client.get("/api/market-data/indicator-series",
                          params={"ticker": "ARM", "indicator": "sma", "period": 20,
                                  "start": "2023-01-01", "end": "2023-02-01"})

    assert response.status_code == 200 and response.json()["points"] == []


def test_route_maps_errors_and_answers_in_camel_case(fmp, client):
    params = {"ticker": "AAPL", "indicator": "adx", "period": 14,
              "start": "2026-09-01", "end": "2026-09-30"}
    fmp["values"] = {date(2026, 9, 1): 25.0}
    assert client.get("/api/market-data/indicator-series", params=params).json() == {
        "ticker": "AAPL", "indicator": "adx", "period": 14,
        "points": [{"date": "2026-09-01", "value": 25.0}],
    }

    market_data._series_cache.clear()
    fmp["error"] = FMPUnavailable("FMP adx(14) for AAPL failed (HTTP 429).")
    assert client.get("/api/market-data/indicator-series", params=params).status_code == 503

    bad = client.get("/api/market-data/indicator-series", params={**params, "indicator": "macd"})
    assert bad.status_code == 422 and "Unknown indicator" in bad.json()["detail"]


def test_the_series_route_needs_a_signed_in_user():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    response = TestClient(app).get("/api/market-data/indicator-series",
                                   params={"ticker": "AAPL", "indicator": "rsi", "period": 14,
                                           "start": "2026-09-01", "end": "2026-09-30"})
    assert response.status_code == 401


def test_catalogue_lists_every_fetchable_indicator(client):
    items = client.get("/api/market-data/fmp-indicators").json()["items"]

    assert [item["name"] for item in items] == list(TECHNICAL_INDICATORS)
    rsi = next(item for item in items if item["name"] == "rsi")
    assert rsi == {"name": "rsi", "label": "Relative strength index", "shortLabel": "RSI",
                   "pane": "separate", "defaultPeriod": 14, "minPeriod": 2, "maxPeriod": 250,
                   "minValue": 0.0, "maxValue": 100.0}
    assert {item["name"] for item in items if item["pane"] == "price"} == {"sma", "ema", "wma", "dema", "tema"}
