"""FMP exact symbol recognition, caching and authenticated submission enforcement."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.data import fmp
from src.api.dependencies import current_user
from src.api.routes import backtests as backtests_api, market_data as market_data_api
from src.schemas.backtests import BacktestRunRequest
from src.schemas.market_data import CoverageResponse
from src.services import backtests, market_data


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("FMP_API_KEY", "test-secret-never-logged")
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setattr(fmp.time, "sleep", lambda _: None)
    monkeypatch.setattr(market_data, "session_scope", lambda: pytest.fail("Ticker validation must not read market-data DB"))
    monkeypatch.setattr(fmp.FMPMarketData, "get_historical_data", lambda *args: pytest.fail("Symbol lookup must not infer identity from history"))
    market_data._symbol_cache.clear()
    market_data._search_cache.clear()
    # The known-ticker index reads the database; here it is whatever a test
    # says it is, and empty unless one says otherwise.
    market_data.invalidate_known_tickers()
    monkeypatch.setattr(market_data, "load_known_tickers", AsyncMock(return_value={}))
    monkeypatch.setattr(market_data, "settings", replace(market_data.settings, symbol_search_yahoo_fallback=True))
    # No test reaches the real Yahoo: by default it is simply down, and a
    # test that wants it answering stubs the transport itself.
    def yahoo_down(*args, **kwargs):
        raise URLError("stubbed")
    monkeypatch.setattr(market_data.yahoo, "urlopen", yahoo_down)
    yield
    market_data._symbol_cache.clear()
    market_data._search_cache.clear()
    market_data.invalidate_known_tickers()


def transport(monkeypatch, payload):
    calls = []
    def open_url(url, *, timeout):
        parsed = urlparse(url)
        calls.append((parsed.scheme, parsed.netloc, parsed.path, parse_qs(parsed.query), timeout))
        rows = payload(parse_qs(parsed.query)["query"][0]) if callable(payload) else payload
        return io.BytesIO(json.dumps(rows).encode())
    monkeypatch.setattr(fmp, "urlopen", open_url)
    return calls


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.include_router(backtests_api.router, prefix="/api")
    app.dependency_overrides[current_user.require_current_user] = lambda: uuid.UUID(int=1)
    with TestClient(app) as client:
        yield client


def test_authenticated_contract_normalizes_and_deduplicates_exact_matches(api, monkeypatch):
    calls = transport(monkeypatch, lambda ticker: [{"symbol": ticker + "X"}, {"symbol": ticker}] if ticker != "XZCER" else [])
    response = api.get("/api/market-data/validate-tickers", params={"tickers": " aapl,XZCER,AAPL,^gspc,btcusd"})
    assert response.status_code == 200
    assert response.json() == {
        "tickers": [{"ticker": "AAPL", "status": "valid"}, {"ticker": "XZCER", "status": "unknown"},
                    {"ticker": "^GSPC", "status": "valid"}, {"ticker": "BTCUSD", "status": "valid"}],
        "unknown": ["XZCER"],
    }
    assert len(calls) == 4
    assert all(c[:3] == ("https", "financialmodelingprep.com", "/stable/search-symbol") for c in calls)
    assert all(c[3]["apikey"] == ["test-secret-never-logged"] and c[3]["limit"] == ["100"] and c[4] == 6 for c in calls)


def test_anonymous_identity_header_is_rejected_before_fmp(monkeypatch):
    calls = transport(monkeypatch, [])
    monkeypatch.setattr(current_user, "settings", SimpleNamespace(app_env="production", auth_allow_dev_identity=False,
                        auth_cognito_issuer="", auth_cognito_client_id="", temporary_user_id=str(uuid.UUID(int=1))))
    app = FastAPI();app.include_router(market_data_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get("/api/market-data/validate-tickers?tickers=AAPL", headers={"X-User-Id": str(uuid.UUID(int=1))})
    assert response.status_code == 401 and calls == []


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": str(uuid.UUID(int=7))}])
def test_development_identity_bypass_still_reaches_the_lookup(monkeypatch, headers):
    # AUTH_ALLOW_DEV_IDENTITY=true with APP_ENV=development and no Cognito
    # settings: no bearer token, the temporary user or X-User-Id stands in.
    calls = transport(monkeypatch, lambda ticker: [{"symbol": ticker}])
    monkeypatch.setattr(current_user, "settings", SimpleNamespace(app_env="development", auth_allow_dev_identity=True,
                        auth_cognito_issuer="", auth_cognito_client_id="", temporary_user_id=str(uuid.UUID(int=1))))

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(current_user, "session_scope", scope)
    monkeypatch.setattr(current_user.user_creds_repo, "get_user",
                        AsyncMock(side_effect=lambda session, owner_id: SimpleNamespace(id=owner_id)))
    app = FastAPI();app.include_router(market_data_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get("/api/market-data/validate-tickers?tickers=AAPL", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"tickers": [{"ticker": "AAPL", "status": "valid"}], "unknown": []}
    assert len(calls) == 1


@pytest.mark.parametrize("symbols", ["", " ", "AAPL,", "AAPL,,MSFT", "AAPL/BAD", "@AAPL", "A" * 21, ",".join(["A"] * 51)])
def test_invalid_input_is_rejected_without_provider_requests(api, monkeypatch, symbols):
    calls = transport(monkeypatch, [])
    assert api.get("/api/market-data/validate-tickers", params={"tickers": symbols}).status_code == 422
    assert calls == []


def test_prefix_match_is_not_exact_symbol_recognition(monkeypatch):
    transport(monkeypatch, [{"symbol": "AAPL"}, {"symbol": "AAPL.L"}])
    assert fmp.FMPMarketData().symbol_exists("AAP") is False


@pytest.mark.parametrize("payload", [None, {"Error Message": "test-secret-never-logged"}, [None], [{"name": "Missing symbol"}], [{"symbol": ""}]])
def test_bad_metadata_is_503_and_not_cached(api, monkeypatch, payload):
    calls = transport(monkeypatch, payload)
    for _ in range(2):
        response = api.get("/api/market-data/validate-tickers?tickers=AAPL")
        assert response.status_code == 503
        assert "test-secret-never-logged" not in response.text
    assert len(calls) == 2 and not market_data._symbol_cache


def test_failed_lookup_can_recover_and_oversized_metadata_is_rejected(api, monkeypatch):
    transport(monkeypatch, [{"symbol": "AAPL", "oversized": "x" * 512_000}])
    assert api.get("/api/market-data/validate-tickers?tickers=AAPL").status_code == 503
    assert not market_data._symbol_cache
    calls = transport(monkeypatch, [{"symbol": "AAPL"}])
    assert api.get("/api/market-data/validate-tickers?tickers=AAPL").json()["unknown"] == []
    assert len(calls) == 1


def test_truncated_search_without_exact_match_is_inconclusive_not_unknown(api, monkeypatch):
    rows = [{"symbol": f"A{index}"} for index in range(fmp.SYMBOL_SEARCH_LIMIT)]
    transport(monkeypatch, rows)
    response = api.get("/api/market-data/validate-tickers?tickers=A")
    assert response.status_code == 503 and "incomplete" in response.json()["detail"]
    assert not market_data._symbol_cache
    rows[0] = {"symbol": "A"}
    assert api.get("/api/market-data/validate-tickers?tickers=A").json()["unknown"] == []


@pytest.mark.parametrize("code,attempts", [(401, 1), (402, 1), (403, 1), (404, 1), (429, 2), (503, 2)])
def test_provider_http_failure_never_means_unknown(api, monkeypatch, caplog, code, attempts):
    calls = []
    def failure(url, **kwargs):
        calls.append(1)
        raise HTTPError(url, code, "test-secret-never-logged", {}, io.BytesIO(b"test-secret-never-logged"))
    monkeypatch.setattr(fmp, "urlopen", failure)
    response = api.get("/api/market-data/validate-tickers?tickers=AAPL")
    assert response.status_code == 503 and len(calls) == attempts
    assert "test-secret-never-logged" not in response.text + caplog.text
    assert not market_data._symbol_cache


def test_timeout_and_missing_credentials_remain_provider_errors(api, monkeypatch):
    calls = []
    def timeout(*args, **kwargs):
        calls.append(1);raise URLError("test-secret-never-logged")
    monkeypatch.setattr(fmp, "urlopen", timeout)
    assert api.get("/api/market-data/validate-tickers?tickers=AAPL").status_code == 503
    assert len(calls) == 2 and not market_data._symbol_cache
    monkeypatch.setenv("FMP_API_KEY", "")
    assert api.get("/api/market-data/validate-tickers?tickers=AAPL").status_code == 503
    assert len(calls) == 2


def test_successful_cache_ttls_and_eviction_are_bounded(monkeypatch):
    now = [100.0];calls = []
    monkeypatch.setattr(market_data, "_symbol_clock", lambda: now[0])
    def exists(self, ticker):
        calls.append(ticker);return ticker != "UNKNOWN"
    monkeypatch.setattr(fmp.FMPMarketData, "symbol_exists", exists)
    for _ in range(2):
        assert market_data._fmp_symbol_exists("AAPL") is True
        assert market_data._fmp_symbol_exists("UNKNOWN") is False
    assert calls == ["AAPL", "UNKNOWN"]
    now[0] += 301
    market_data._fmp_symbol_exists("AAPL");market_data._fmp_symbol_exists("UNKNOWN")
    assert calls == ["AAPL", "UNKNOWN", "UNKNOWN"]
    now[0] += 3300
    market_data._fmp_symbol_exists("AAPL")
    assert calls[-1] == "AAPL" and len(calls) == 4
    for index in range(520):market_data._fmp_symbol_exists(f"T{index}")
    assert len(market_data._symbol_cache) == 512


def test_concurrent_same_symbol_is_coalesced_and_provider_concurrency_is_four(monkeypatch):
    lock = threading.Lock();release = threading.Event();entered = threading.Event()
    counts = {"calls": 0, "active": 0, "peak": 0}
    def exists(self, ticker):
        with lock:
            counts["calls"] += 1;counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            entered.set()
        assert release.wait(3), "Test failed to release provider calls"
        with lock:counts["active"] -= 1
        return True
    monkeypatch.setattr(fmp.FMPMarketData, "symbol_exists", exists)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(market_data._fmp_symbol_exists, "AAPL") for _ in range(8)]
        assert entered.wait(2)
        threading.Event().wait(0.05)
        assert counts["calls"] == 1
        release.set()
        assert all(f.result(timeout=3) for f in futures)
    assert counts["calls"] == 1
    release.clear();entered.clear();counts.update(calls=0, active=0, peak=0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(market_data._fmp_symbol_exists, f"T{index}") for index in range(8)]
        deadline = time.monotonic() + 2
        while counts["active"] < 4 and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        threading.Event().wait(0.05)
        assert counts["active"] == 4
        release.set()
        assert all(f.result(timeout=3) for f in futures)
    assert counts["calls"] == 8 and counts["peak"] == 4


def _submission(monkeypatch):
    monkeypatch.setattr(backtests, "_load_runnable_strategy", AsyncMock(return_value=backtests._RunnableStrategy("sample", ["AAPL"], [])))
    coverage = AsyncMock(return_value=CoverageResponse(tickers=[], start="2025-01-01", end="2025-12-31"))
    create = AsyncMock(return_value=SimpleNamespace(id="test-run"));dispatch = AsyncMock(return_value="queued")
    monkeypatch.setattr(market_data, "coverage_for", coverage)
    monkeypatch.setattr(backtests, "create_backtest_run", create);monkeypatch.setattr(backtests, "_dispatch", dispatch)
    request = BacktestRunRequest(name="validated universe", strategy_key="sample", start_date="2025-01-02", end_date="2025-01-31", initial_capital=10000, params={"universe": ["XZCER"]})
    return request, coverage, create, dispatch


def test_direct_submission_rejects_unknown_selected_universe_before_history_or_queue(monkeypatch):
    calls = transport(monkeypatch, [])
    request, coverage, create, dispatch = _submission(monkeypatch)
    with pytest.raises(backtests.RunSubmissionError, match="does not recognize.*XZCER"):
        asyncio.run(backtests.submit_backtest_run(request, owner_id=uuid.UUID(int=1)))
    assert calls[0][3]["query"] == ["XZCER"]
    coverage.assert_not_awaited();create.assert_not_awaited();dispatch.assert_not_awaited()


def test_unknown_submitted_symbol_is_422_at_api_boundary(api, monkeypatch):
    transport(monkeypatch, [])
    request, coverage, create, dispatch = _submission(monkeypatch)
    response = api.post("/api/backtests", json=request.model_dump(mode="json", by_alias=True))
    assert response.status_code == 422 and "does not recognize" in response.json()["detail"]
    assert "XZCER" in response.json()["detail"]
    coverage.assert_not_awaited();create.assert_not_awaited();dispatch.assert_not_awaited()


def test_valid_symbol_with_no_history_remains_a_distinct_window_error(monkeypatch):
    transport(monkeypatch, [{"symbol": "XZCER"}])
    request, coverage, create, dispatch = _submission(monkeypatch)
    coverage.return_value = CoverageResponse(tickers=[], start=None, end=None, missing=["XZCER"])
    with pytest.raises(backtests.RunSubmissionError, match="no available market-data history.*XZCER"):
        asyncio.run(backtests.submit_backtest_run(request))
    coverage.assert_awaited_once();create.assert_not_awaited();dispatch.assert_not_awaited()


def test_recognized_universe_reaches_execution_with_its_owner(monkeypatch):
    transport(monkeypatch, [{"symbol": "XZCER"}])
    request, coverage, create, dispatch = _submission(monkeypatch)
    assert asyncio.run(backtests.submit_backtest_run(request, owner_id=uuid.UUID(int=1))) == "queued"
    assert create.await_args.kwargs["owner_id"] == uuid.UUID(int=1)
    assert create.await_args.kwargs["params"]["universe"] == ["XZCER"]
    coverage.assert_awaited_once();dispatch.assert_awaited_once()


def test_provider_failure_during_submission_is_503_before_history_and_queue(api, monkeypatch):
    transport(monkeypatch, {"Error Message": "test-secret-never-logged"})
    request, coverage, create, dispatch = _submission(monkeypatch)
    response = api.post("/api/backtests", json=request.model_dump(mode="json", by_alias=True))
    assert response.status_code == 503 and "test-secret-never-logged" not in response.text
    coverage.assert_not_awaited();create.assert_not_awaited();dispatch.assert_not_awaited()


def test_explicit_legacy_database_proof_does_not_contact_fmp(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    monkeypatch.setenv("FMP_API_KEY", "")
    request, coverage, create, dispatch = _submission(monkeypatch)
    assert asyncio.run(backtests.submit_backtest_run(request)) == "queued"
    coverage.assert_awaited_once();create.assert_awaited_once();dispatch.assert_awaited_once()


def test_the_status_vocabulary_is_closed():
    # The run form switches on this enum; a provider failure is a 503, never
    # a third status the form has no branch for.
    from pydantic import ValidationError

    from src.schemas.market_data import TickerValidation

    assert TickerValidation(ticker="AAPL", status="valid").status == "valid"
    with pytest.raises(ValidationError):
        TickerValidation(ticker="AAPL", status="error")
    assert TickerValidation.model_json_schema()["properties"]["status"]["enum"] == ["valid", "unknown"]


# --- symbol search: suggestions while a ticker is typed -----------------------

SEARCH = "/api/market-data/search-symbols"


def rows(*symbols, **extra):
    return [
        {"symbol": symbol, "name": f"{symbol} Inc", "exchange": "NASDAQ", "exchangeFullName": "NASDAQ",
         "currency": "USD", **extra}
        for symbol in symbols
    ]


def test_search_answers_matches_in_provider_order_with_names_and_exchanges(api, monkeypatch):
    calls = transport(monkeypatch, rows("MSFT", "MSTR"))

    response = api.get(SEARCH, params={"query": " ms "})

    assert response.status_code == 200
    assert response.json() == {
        "matches": [
            {"symbol": "MSFT", "name": "MSFT Inc", "exchange": "NASDAQ", "source": "fmp"},
            {"symbol": "MSTR", "name": "MSTR Inc", "exchange": "NASDAQ", "source": "fmp"},
        ],
        "truncated": False,
        "providerError": None,
    }
    (call,) = calls
    assert call[:3] == ("https", "financialmodelingprep.com", "/stable/search-symbol")
    # One more than shown, so a full page is known to be a cut; the query is
    # normalized before it reaches the provider.
    assert call[3]["query"] == ["MS"] and call[3]["limit"] == ["11"]
    assert call[3]["apikey"] == ["test-secret-never-logged"] and call[4] == 6


def test_search_tolerates_missing_metadata_but_not_a_missing_symbol(api, monkeypatch):
    transport(monkeypatch, [{"symbol": "MSFT", "exchange": "NASDAQ"}, {"symbol": "MSTR", "name": 7, "exchange": "NASDAQ"}])
    response = api.get(SEARCH, params={"query": "MS"})
    assert response.status_code == 200
    assert response.json()["matches"] == [
        {"symbol": "MSFT", "name": None, "exchange": "NASDAQ", "source": "fmp"},
        {"symbol": "MSTR", "name": None, "exchange": "NASDAQ", "source": "fmp"},
    ]

    market_data._search_cache.clear()
    calls = transport(monkeypatch, [{"symbol": "MSFT"}, {"name": "no symbol"}])
    assert api.get(SEARCH, params={"query": "MS"}).status_code == 503
    # Not cached: the next request asks the provider again.
    assert api.get(SEARCH, params={"query": "MS"}).status_code == 503
    assert len(calls) == 2


def test_search_marks_a_full_page_as_truncated_and_shows_ten(api, monkeypatch):
    transport(monkeypatch, rows(*(f"A{i:02d}" for i in range(11))))

    body = api.get(SEARCH, params={"query": "A"}).json()

    assert body["truncated"] is True
    assert [m["symbol"] for m in body["matches"]] == [f"A{i:02d}" for i in range(10)]


def test_search_pages_are_cached_by_prefix_until_they_expire(api, monkeypatch):
    calls = transport(monkeypatch, rows("MSFT"))
    now = [1000.0]
    monkeypatch.setattr(market_data, "_symbol_clock", lambda: now[0])

    assert api.get(SEARCH, params={"query": "MS"}).status_code == 200
    assert api.get(SEARCH, params={"query": "ms"}).status_code == 200
    assert len(calls) == 1, "same prefix, differently typed, is one provider call"

    now[0] += market_data._SEARCH_TTL_SECONDS + 1
    assert api.get(SEARCH, params={"query": "MS"}).status_code == 200
    assert len(calls) == 2


def test_concurrent_same_prefix_searches_are_coalesced(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def open_url(url, *, timeout):
        calls.append(url)
        started.set()
        release.wait(5)
        return io.BytesIO(json.dumps(rows("MSFT")).encode())

    monkeypatch.setattr(fmp, "urlopen", open_url)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(market_data._fmp_search_symbols, "MS") for _ in range(6)]
        assert started.wait(5)
        time.sleep(0.05)
        release.set()
        results = [future.result(5) for future in futures]
    assert len(calls) == 1
    assert all(result == results[0] for result in results)


def test_a_suggested_symbol_validates_without_a_second_provider_call(api, monkeypatch):
    calls = transport(monkeypatch, lambda query: rows("MSFT", "MSTR") if query == "MS" else rows(query))

    assert api.get(SEARCH, params={"query": "MS"}).status_code == 200
    validated = api.get("/api/market-data/validate-tickers", params={"tickers": "MSFT"})
    assert validated.json()["unknown"] == []
    assert len(calls) == 1, "the page already proved MSFT exists"

    # A symbol the page did not list still has to be looked up.
    api.get("/api/market-data/validate-tickers", params={"tickers": "AAPL"})
    assert len(calls) == 2


def test_anonymous_search_is_rejected_before_fmp(monkeypatch):
    calls = transport(monkeypatch, rows("MSFT"))
    monkeypatch.setattr(current_user, "settings", SimpleNamespace(app_env="production", auth_allow_dev_identity=False,
                        auth_cognito_issuer="", auth_cognito_client_id="", temporary_user_id=str(uuid.UUID(int=1))))
    app = FastAPI();app.include_router(market_data_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get(SEARCH, params={"query": "MS"})
    assert response.status_code == 401 and calls == []


@pytest.mark.parametrize("query", ["", " ", "A" * 21, "M S", "MS/", "@MS"])
def test_invalid_search_input_is_rejected_without_provider_requests(api, monkeypatch, query):
    calls = transport(monkeypatch, rows("MSFT"))
    assert api.get(SEARCH, params={"query": query}).status_code == 422
    assert calls == []


def test_search_provider_failure_is_503_and_retried_next_time(api, monkeypatch):
    calls = []

    def fail(url, *, timeout):
        calls.append(url)
        raise HTTPError(url, 503, "test-secret-never-logged", {}, io.BytesIO(b""))

    monkeypatch.setattr(fmp, "urlopen", fail)
    first = api.get(SEARCH, params={"query": "MS"})
    assert first.status_code == 503 and "test-secret-never-logged" not in first.text
    second = api.get(SEARCH, params={"query": "MS"})
    assert second.status_code == 503
    # Two requests, each with the provider's one transient retry: nothing cached.
    assert len(calls) == 4


# --- the fallback chain: known tickers → FMP (symbol ∪ name, US only) → Yahoo ---

def known(**tickers):
    market_data.invalidate_known_tickers()
    return AsyncMock(return_value=tickers)


def test_known_tickers_come_first_and_cost_no_provider_call_when_they_fill_the_page(api, monkeypatch):
    calls = transport(monkeypatch, rows("MSFT"))
    monkeypatch.setattr(market_data, "load_known_tickers", known(**{f"MS{i:02d}": "database" for i in range(12)}))

    body = api.get(SEARCH, params={"query": "MS"}).json()

    assert [m["source"] for m in body["matches"]] == ["database"] * 10
    assert body["truncated"] is True and calls == []


def test_known_tickers_lead_and_the_provider_fills_the_rest_without_repeats(api, monkeypatch):
    transport(monkeypatch, rows("MSFT", "MSTR"))
    monkeypatch.setattr(market_data, "load_known_tickers", known(MSFT="database", MSCI="run"))

    body = api.get(SEARCH, params={"query": "MS"}).json()

    assert [(m["symbol"], m["source"]) for m in body["matches"]] == [
        ("MSCI", "run"), ("MSFT", "database"), ("MSTR", "fmp"),
    ]


def test_a_known_ticker_validates_without_the_provider(api, monkeypatch):
    calls = transport(monkeypatch, lambda query: rows(query))
    monkeypatch.setattr(market_data, "load_known_tickers", known(MSFT="database"))

    body = api.get("/api/market-data/validate-tickers", params={"tickers": "MSFT,AAPL"}).json()

    assert body["unknown"] == []
    assert [c[3]["query"] for c in calls] == [["AAPL"]], "only the unknown symbol reached FMP"


def test_company_name_search_joins_in_from_three_letters_and_foreign_listings_are_dropped(api, monkeypatch):
    by_name = [
        {"symbol": "MSF.F", "name": "Microsoft Corporation", "exchange": "FSX", "currency": "EUR"},
        {"symbol": "MSFT", "name": "Microsoft Corporation", "exchange": "NASDAQ", "currency": "USD"},
        {"symbol": "4338.HK", "name": "Microsoft Corporation", "exchange": "HKSE", "currency": "HKD"},
    ]
    paths = []

    def open_url(url, *, timeout):
        path = urlparse(url).path
        paths.append(path)
        return io.BytesIO(json.dumps(by_name if path.endswith("search-name") else rows("MICROS")).encode())

    monkeypatch.setattr(fmp, "urlopen", open_url)

    body = api.get(SEARCH, params={"query": "mic"}).json()

    assert paths == ["/stable/search-symbol", "/stable/search-name"]
    assert [m["symbol"] for m in body["matches"]] == ["MICROS", "MSFT"]

    market_data._search_cache.clear(); paths.clear()
    api.get(SEARCH, params={"query": "mi"})
    assert paths == ["/stable/search-symbol"], "two letters is too short for a name search"


def yahoo_transport(monkeypatch, quotes):
    calls = []

    def open_url(request, *, timeout):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps({"quotes": quotes}).encode())

    monkeypatch.setattr(market_data.yahoo, "urlopen", open_url)
    return calls


def test_yahoo_answers_when_fmp_cannot_and_keeps_only_us_equities(api, monkeypatch):
    def fail(url, *, timeout):
        raise HTTPError(url, 503, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(fmp, "urlopen", fail)
    yahoo_calls = yahoo_transport(monkeypatch, [
        {"symbol": "MSFT", "shortname": "Microsoft", "exchDisp": "NASDAQ", "quoteType": "EQUITY"},
        {"symbol": "MSFT.MX", "shortname": "Microsoft", "exchDisp": "Mexico", "quoteType": "EQUITY"},
        {"symbol": "MS=F", "shortname": "Futures", "exchDisp": "NYMEX", "quoteType": "FUTURE"},
    ])

    body = api.get(SEARCH, params={"query": "MS"}).json()

    assert [(m["symbol"], m["source"]) for m in body["matches"]] == [("MSFT", "yahoo")]
    assert body["providerError"] is None and len(yahoo_calls) == 1
    # Yahoo never vouches for a symbol: the exact lookup still goes to FMP.
    assert "MSFT" not in market_data._symbol_cache


def test_known_tickers_still_answer_when_every_provider_is_down(api, monkeypatch):
    def fail(url, *, timeout):
        raise HTTPError(url, 503, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(fmp, "urlopen", fail)
    monkeypatch.setattr(market_data.yahoo, "urlopen", fail)
    monkeypatch.setattr(market_data, "load_known_tickers", known(MSFT="database"))

    response = api.get(SEARCH, params={"query": "MS"})

    assert response.status_code == 200
    body = response.json()
    assert [m["symbol"] for m in body["matches"]] == ["MSFT"]
    assert "HTTP 503" in body["providerError"]

    market_data.invalidate_known_tickers()
    monkeypatch.setattr(market_data, "load_known_tickers", known())
    assert api.get(SEARCH, params={"query": "MS"}).status_code == 503, "nothing to show is still an outage"


def test_yahoo_fallback_can_be_switched_off(api, monkeypatch):
    def fail(url, *, timeout):
        raise HTTPError(url, 503, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(fmp, "urlopen", fail)
    yahoo_calls = yahoo_transport(monkeypatch, [{"symbol": "MSFT", "exchDisp": "NASDAQ", "quoteType": "EQUITY"}])
    monkeypatch.setattr(market_data, "settings", replace(market_data.settings, symbol_search_yahoo_fallback=False))

    assert api.get(SEARCH, params={"query": "MS"}).status_code == 503
    assert yahoo_calls == []


def test_the_known_index_is_reread_after_its_ttl_and_on_invalidation(monkeypatch):
    loader = known(MSFT="database")
    monkeypatch.setattr(market_data, "load_known_tickers", loader)
    now = [1000.0]
    monkeypatch.setattr(market_data, "_symbol_clock", lambda: now[0])

    assert asyncio.run(market_data.known_tickers()) == {"MSFT": "database"}
    assert asyncio.run(market_data.known_tickers()) == {"MSFT": "database"}
    assert loader.await_count == 1
    now[0] += market_data._KNOWN_TTL_SECONDS + 1
    asyncio.run(market_data.known_tickers())
    assert loader.await_count == 2
    market_data.invalidate_known_tickers()
    asyncio.run(market_data.known_tickers())
    assert loader.await_count == 3
