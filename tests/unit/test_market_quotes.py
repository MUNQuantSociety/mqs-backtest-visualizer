"""Latest quotes for the Stock Screener: FMP batch-quote, profiles, caching, route.

No network: FMP is replaced by recorded answers in every test.
"""

import asyncio
import io
import json
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.data import fmp
from engine.data.fmp import FMPUnavailable
from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_data as market_data_api
from src.services import market_data


def fmp_row(symbol="AAPL", price=347.725, **overrides):
    """One batch-quote row as FMP answers it."""
    row = {
        "symbol": symbol, "name": f"{symbol} Inc.", "exchange": "NASDAQ", "price": price,
        "change": -0.495, "changePercentage": -0.14215, "open": 348.0, "previousClose": 348.22,
        "dayHigh": 350.1, "dayLow": 346.0, "yearHigh": 351.73, "yearLow": 243.42,
        "volume": 39539690, "marketCap": 5074761240500, "priceAvg50": 330.5,
        "priceAvg200": 290.1, "timestamp": 1792785601,
    }
    return {**row, **overrides}


# --- engine: FMPMarketData.get_quotes / is_etf ---------------------------------

@pytest.fixture
def fmp_http(monkeypatch):
    """Answers FMP requests with a payload a test sets, and records the queries."""
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp.time, "sleep", lambda _: None)
    state = {"payload": [], "calls": []}

    def open_url(url, *, timeout):
        parsed = urlparse(url)
        state["calls"].append((parsed.path, parse_qs(parsed.query)))
        return io.StringIO(json.dumps(state["payload"]))

    monkeypatch.setattr(fmp, "urlopen", open_url)
    return state


def test_quotes_are_parsed_with_change_as_a_ratio(fmp_http):
    fmp_http["payload"] = [fmp_row()]

    (quote,) = fmp.FMPMarketData().get_quotes(["aapl"])

    assert quote["ticker"] == "AAPL" and quote["price"] == 347.725
    assert quote["change_ratio"] == pytest.approx(-0.0014215)
    assert quote["previous_close"] == 348.22 and quote["year_high"] == 351.73
    assert quote["market_cap"] == 5074761240500 and quote["average_200"] == 290.1
    assert quote["as_of"] == datetime.fromtimestamp(1792785601, tz=timezone.utc)
    path, query = fmp_http["calls"][0]
    assert path == "/stable/batch-quote" and query["symbols"] == ["AAPL"]


def test_one_request_for_many_symbols_deduplicated(fmp_http):
    fmp_http["payload"] = [fmp_row("AAPL"), fmp_row("SPY")]

    fmp.FMPMarketData().get_quotes(["AAPL", " spy", "AAPL"])

    assert len(fmp_http["calls"]) == 1
    assert fmp_http["calls"][0][1]["symbols"] == ["AAPL,SPY"]


def test_empty_fields_stay_empty(fmp_http):
    fmp_http["payload"] = [fmp_row(marketCap=None, priceAvg50="", name=None, timestamp=None)]

    (quote,) = fmp.FMPMarketData().get_quotes(["AAPL"])

    assert quote["market_cap"] is None and quote["average_50"] is None
    assert quote["name"] is None and quote["as_of"] is None


@pytest.mark.parametrize("bad", [
    {"price": None}, {"price": 0}, {"price": "abc"}, {"volume": "nan"}, {"symbol": ""},
])
def test_a_malformed_row_is_dropped_not_fatal(fmp_http, bad):
    fmp_http["payload"] = [{**fmp_row("AAPL"), **bad}, fmp_row("SPY")]

    quotes = fmp.FMPMarketData().get_quotes(["AAPL", "SPY"])

    assert [quote["ticker"] for quote in quotes] == ["SPY"]


def test_rows_for_symbols_not_asked_for_are_ignored(fmp_http):
    fmp_http["payload"] = [fmp_row("AAPL"), fmp_row("MSFT")]

    assert [q["ticker"] for q in fmp.FMPMarketData().get_quotes(["AAPL"])] == ["AAPL"]


@pytest.mark.parametrize("tickers", [[], ["  "], [f"T{i}" for i in range(51)]])
def test_bad_ticker_lists_fail_before_any_request(fmp_http, tickers):
    with pytest.raises(ValueError):
        fmp.FMPMarketData().get_quotes(tickers)
    assert fmp_http["calls"] == []


def test_provider_errors_keep_their_advice_and_hide_the_key(fmp_http, monkeypatch):
    def refuse(url, *, timeout):
        raise HTTPError(url, 402, "Payment Required", {}, io.BytesIO(b""))

    monkeypatch.setattr(fmp, "urlopen", refuse)

    with pytest.raises(FMPUnavailable) as error:
        fmp.FMPMarketData().get_quotes(["AAPL"])
    assert "HTTP 402" in str(error.value) and "test-secret-key" not in str(error.value)


@pytest.mark.parametrize("payload,expected", [
    ([{"symbol": "SPY", "isEtf": True}], True),
    ([{"symbol": "AAPL", "isEtf": False}], False),
    ([], None),
    ([{"symbol": "X"}], None),
])
def test_is_etf_reads_the_profile_flag(fmp_http, payload, expected):
    fmp_http["payload"] = payload

    assert fmp.FMPMarketData().is_etf("SPY") is expected
    path, query = fmp_http["calls"][0]
    assert path == "/stable/profile" and query["symbol"] == ["SPY"]


# --- service: quotes_for --------------------------------------------------------

@pytest.fixture
def provider(monkeypatch):
    """A fake FMP for the service, with a clock the test moves."""
    state = {"quotes": {}, "etfs": set(), "quote_calls": [], "profile_calls": [],
             "error": None, "profile_error": None, "now": 1000.0}

    class FakeProvider:
        def get_quotes(self, tickers):
            state["quote_calls"].append(list(tickers))
            if state["error"]:
                raise state["error"]
            return [fmp._parse_quote(state["quotes"][t]) for t in tickers if t in state["quotes"]]

        def is_etf(self, ticker):
            state["profile_calls"].append(ticker)
            if state["profile_error"]:
                raise state["profile_error"]
            return ticker in state["etfs"]

    monkeypatch.setattr(market_data, "FMPMarketData", FakeProvider)
    monkeypatch.setattr(market_data, "_quote_clock", lambda: state["now"])
    market_data._quote_cache.clear()
    market_data._kind_cache.clear()
    yield state
    market_data._quote_cache.clear()
    market_data._kind_cache.clear()


def quotes(*tickers):
    return asyncio.run(market_data.quotes_for(list(tickers)))


def test_quotes_come_back_in_the_order_asked_with_missing_named(provider):
    provider["quotes"] = {t: fmp_row(t) for t in ("NVDA", "AAPL")}

    result = quotes("nvda", "ZZZZ", "AAPL")

    assert [q.ticker for q in result.quotes] == ["NVDA", "AAPL"]
    assert result.missing == ["ZZZZ"]


def test_kinds_come_from_the_exchange_and_the_profile(provider):
    provider["quotes"] = {"AAPL": fmp_row("AAPL"), "SPY": fmp_row("SPY", exchange="AMEX"),
                          "BTCUSD": fmp_row("BTCUSD", exchange="CRYPTO"),
                          "^VIX": fmp_row("^VIX", exchange="INDEX", marketCap=0, volume=0)}
    provider["etfs"] = {"SPY"}

    result = quotes("AAPL", "SPY", "BTCUSD", "^VIX")

    assert {q.ticker: q.kind for q in result.quotes} == {
        "AAPL": "stock", "SPY": "etf", "BTCUSD": "crypto", "^VIX": "index",
    }
    # Crypto and indices are known from the exchange; no profile is asked for.
    assert sorted(provider["profile_calls"]) == ["AAPL", "SPY"]


def test_a_fresh_quote_is_not_fetched_again_and_only_new_tickers_are(provider):
    provider["quotes"] = {t: fmp_row(t) for t in ("AAPL", "SPY")}

    quotes("AAPL")
    quotes("AAPL", "SPY")

    assert provider["quote_calls"] == [["AAPL"], ["SPY"]]


def test_quotes_are_refetched_after_a_minute_but_kinds_after_a_day(provider):
    provider["quotes"] = {"AAPL": fmp_row("AAPL")}

    quotes("AAPL")
    provider["now"] += market_data._QUOTE_TTL_SECONDS + 1
    quotes("AAPL")

    assert len(provider["quote_calls"]) == 2
    assert provider["profile_calls"] == ["AAPL"]


def test_a_missing_symbol_is_remembered_for_the_minute(provider):
    quotes("ZZZZ")
    quotes("ZZZZ")

    assert provider["quote_calls"] == [["ZZZZ"]]


def test_an_unreadable_profile_shows_a_stock_and_is_asked_again(provider):
    provider["quotes"] = {"SPY": fmp_row("SPY")}
    provider["profile_error"] = FMPUnavailable("FMP profile for SPY failed (HTTP 429).")

    assert quotes("SPY").quotes[0].kind == "stock"
    provider["profile_error"] = None
    provider["etfs"] = {"SPY"}
    provider["now"] += market_data._QUOTE_TTL_SECONDS + 1
    assert quotes("SPY").quotes[0].kind == "etf"


@pytest.mark.parametrize("tickers", [[], ["not a ticker!"], [f"T{i}" for i in range(51)]])
def test_bad_ticker_lists_are_refused_before_fmp(provider, tickers):
    with pytest.raises(ValueError):
        asyncio.run(market_data.quotes_for(tickers))
    assert provider["quote_calls"] == []


# --- route ------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_route_answers_in_camel_case(provider, client):
    provider["quotes"] = {"AAPL": fmp_row("AAPL")}

    body = client.get("/api/market-data/quotes", params={"tickers": "AAPL,ZZZZ"}).json()

    assert body["missing"] == ["ZZZZ"]
    assert body["quotes"][0] == {
        "ticker": "AAPL", "name": "AAPL Inc.", "exchange": "NASDAQ", "kind": "stock",
        "price": 347.725, "change": -0.495, "changeRatio": pytest.approx(-0.0014215),
        "open": 348.0, "previousClose": 348.22, "dayHigh": 350.1, "dayLow": 346.0,
        "yearHigh": 351.73, "yearLow": 243.42, "volume": 39539690.0,
        "marketCap": 5074761240500.0, "average50": 330.5, "average200": 290.1,
        "asOf": "2026-10-23T20:00:01+00:00",
    }


def test_route_maps_bad_input_and_provider_failures(provider, client):
    bad = client.get("/api/market-data/quotes", params={"tickers": "not a ticker!"})
    assert bad.status_code == 422 and isinstance(bad.json()["detail"], str)

    provider["error"] = FMPUnavailable("FMP quotes for 1 symbols failed (HTTP 429).")
    down = client.get("/api/market-data/quotes", params={"tickers": "AAPL"})
    assert down.status_code == 503 and "429" in down.json()["detail"]


def test_route_needs_a_signed_in_user():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")

    assert TestClient(app).get("/api/market-data/quotes", params={"tickers": "AAPL"}).status_code == 401
