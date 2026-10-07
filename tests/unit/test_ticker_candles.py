"""A ticker's daily OHLCV candles, for the Build tab's chart.

Read straight from FMP: the chart is for any listed symbol, not only the
ones the market-data store holds.
"""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.data.fmp import FMPSymbolUnknown, FMPUnavailable

from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_data as market_data_api
from src.services import market_data


def _bar(day, close):
    return {
        "ticker": "AAPL", "date": day, "open_price": close - 1, "high_price": close + 2,
        "low_price": close - 2, "close_price": close, "volume": 1_000.0,
    }


@pytest.fixture
def fmp(monkeypatch):
    """Records FMP history calls and answers with the rows a test sets."""
    state = {"rows": [], "calls": [], "error": None}

    class FakeProvider:
        def get_historical_data(self, tickers, start, end):
            state["calls"].append((tuple(tickers), start, end))
            if state["error"]:
                raise state["error"]
            return state["rows"]

    monkeypatch.setattr(market_data, "FMPMarketData", FakeProvider)
    market_data._candles_cache.clear()
    # The exact-symbol lookup asked only when history comes back empty.
    state["exists"] = True
    state["lookups"] = []

    def symbol_exists(ticker):
        state["lookups"].append(ticker)
        if isinstance(state["exists"], Exception):
            raise state["exists"]
        return state["exists"]

    monkeypatch.setattr(market_data, "_fmp_symbol_exists", symbol_exists)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_returns_ohlcv_candles_oldest_first(fmp):
    # FMP answers newest first.
    fmp["rows"] = [_bar(date(2026, 3, 3), 201.0), _bar(date(2026, 3, 2), 200.0)]

    result = asyncio.run(market_data.candles_between("aapl", date(2026, 3, 1), date(2026, 3, 31)))

    assert result.ticker == "AAPL"
    assert fmp["calls"] == [(("AAPL",), date(2026, 3, 1), date(2026, 3, 31))]
    assert [candle.model_dump() for candle in result.candles] == [
        {"date": "2026-03-02", "open": 199.0, "high": 202.0, "low": 198.0, "close": 200.0, "volume": 1000.0},
        {"date": "2026-03-03", "open": 200.0, "high": 203.0, "low": 199.0, "close": 201.0, "volume": 1000.0},
    ]


def test_asks_fmp_once_for_a_repeated_window(fmp):
    fmp["rows"] = [_bar(date(2026, 3, 2), 200.0)]

    for _ in range(2):
        asyncio.run(market_data.candles_between("AAPL", date(2026, 3, 1), date(2026, 3, 31)))

    assert len(fmp["calls"]) == 1


def test_does_not_cache_a_provider_failure(fmp):
    fmp["error"] = FMPUnavailable("provider down")
    with pytest.raises(FMPUnavailable):
        asyncio.run(market_data.candles_between("AAPL", date(2026, 3, 1), date(2026, 3, 31)))

    fmp["error"] = None
    asyncio.run(market_data.candles_between("AAPL", date(2026, 3, 1), date(2026, 3, 31)))

    assert len(fmp["calls"]) == 2


@pytest.mark.parametrize(
    ("ticker", "start", "end"),
    [
        ("AAPL", date(2026, 3, 31), date(2026, 3, 1)),  # start after end
        ("", date(2026, 3, 1), date(2026, 3, 31)),
        ("AAPL; DROP", date(2026, 3, 1), date(2026, 3, 31)),
        ("AAPL", date(2000, 1, 1), date(2026, 3, 31)),  # more than the span cap
    ],
)
def test_refuses_an_invalid_request(fmp, ticker, start, end):
    with pytest.raises(ValueError):
        asyncio.run(market_data.candles_between(ticker, start, end))
    assert fmp["calls"] == []


def test_route_serializes_camel_case(client, fmp):
    fmp["rows"] = [_bar(date(2026, 3, 2), 200.0)]

    response = client.get("/api/market-data/candles?ticker=AAPL&start=2026-03-01&end=2026-03-31")

    assert response.status_code == 200
    assert response.json() == {
        "ticker": "AAPL",
        "candles": [
            {"date": "2026-03-02", "open": 199.0, "high": 202.0, "low": 198.0, "close": 200.0, "volume": 1000.0}
        ],
    }


@pytest.mark.parametrize(
    ("error", "status"),
    [(FMPSymbolUnknown("no such symbol"), 404), (FMPUnavailable("provider down"), 503)],
)
def test_route_maps_provider_errors(client, fmp, error, status):
    fmp["error"] = error

    response = client.get("/api/market-data/candles?ticker=ZZZZ&start=2026-03-01&end=2026-03-31")

    assert response.status_code == status


def test_route_rejects_an_invalid_window(client, fmp):
    response = client.get("/api/market-data/candles?ticker=AAPL&start=2026-03-31&end=2026-03-01")

    assert response.status_code == 422


def test_an_unknown_symbol_is_reported_as_unknown(fmp):
    # FMP answers a made-up symbol's history with an empty list.
    fmp["exists"] = False

    with pytest.raises(FMPSymbolUnknown):
        asyncio.run(market_data.candles_between("ZZZZQX", date(2026, 3, 1), date(2026, 3, 31)))
    assert fmp["lookups"] == ["ZZZZQX"]


def test_a_real_symbol_with_no_bars_in_the_window_has_no_candles(fmp):
    result = asyncio.run(market_data.candles_between("AAPL", date(1975, 1, 1), date(1975, 3, 31)))

    assert result.candles == []


def test_a_failed_symbol_lookup_still_answers_with_no_candles(fmp):
    fmp["exists"] = FMPUnavailable("provider down")

    result = asyncio.run(market_data.candles_between("AAPL", date(2026, 3, 1), date(2026, 3, 31)))

    assert result.candles == []


def test_the_symbol_is_not_looked_up_when_there_are_candles(fmp):
    fmp["rows"] = [_bar(date(2026, 3, 2), 200.0)]

    asyncio.run(market_data.candles_between("AAPL", date(2026, 3, 1), date(2026, 3, 31)))

    assert fmp["lookups"] == []


def test_route_answers_404_for_an_unknown_symbol(client, fmp):
    fmp["exists"] = False

    response = client.get("/api/market-data/candles?ticker=ZZZZQX&start=2026-03-01&end=2026-03-31")

    assert response.status_code == 404
    assert "ZZZZQX" in response.json()["detail"]
