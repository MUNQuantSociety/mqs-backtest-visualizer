"""FMP exact symbol recognition, caching and authenticated submission enforcement."""

import asyncio
from contextlib import asynccontextmanager
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
    yield
    market_data._symbol_cache.clear()


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
