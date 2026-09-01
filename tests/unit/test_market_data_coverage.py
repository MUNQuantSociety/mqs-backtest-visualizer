"""`GET /api/market-data/coverage` — the window the run form is allowed to offer.

The repository half is one indexed lookup per ticker and needs the live
database; the part worth testing without one is the arithmetic on top of it,
which is where a wrong answer would actually come from. The repository is
therefore stubbed and the intersection is asserted directly.

The route's own argument handling needs neither a database nor a stub, so those
cases drive the real app.

The async service calls run through ``asyncio.run`` rather than an async test
plugin: none is configured, and adding one would mean a new test dependency for
four assertions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from server import app
from src.repositories import strategies as strategies_repo
from src.schemas.market_data import CoverageResponse, TickerCoverage
from src.services import market_data as market_data_service


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _aliases(model: type) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}


@pytest.fixture
def stub_repo(monkeypatch: pytest.MonkeyPatch):
    """Replace the per-ticker lookup, and the session it would have opened."""

    def install(spans: dict[str, tuple[date, date] | None]) -> None:
        async def fake_coverage(_session, tickers):
            return {ticker: spans.get(ticker) for ticker in tickers}

        class _NullSession:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(strategies_repo, "ticker_coverage", fake_coverage)
        monkeypatch.setattr(
            market_data_service, "session_scope", lambda: _NullSession()
        )

    return install


# ---------------------------------------------------------------------------
# The intersection
# ---------------------------------------------------------------------------


def test_window_is_the_intersection_not_the_union(stub_repo) -> None:
    """Latest start, earliest end. A wider window has no prices at one end."""
    stub_repo(
        {
            "AAPL": (date(2019, 11, 11), date(2026, 7, 15)),
            "TLT": (date(2019, 11, 11), date(2025, 11, 7)),
            "WMT": (date(2020, 1, 2), date(2026, 7, 15)),
        }
    )

    result = asyncio.run(market_data_service.coverage_for(["AAPL", "TLT", "WMT"]))

    assert result.start == "2020-01-02", "WMT starts latest"
    assert result.end == "2025-11-07", "TLT ends earliest"
    assert result.missing == []
    assert len(result.tickers) == 3


def test_a_ticker_with_no_bars_removes_the_window(stub_repo) -> None:
    """No window covers a universe one member has no data for."""
    stub_repo({"AAPL": (date(2020, 1, 2), date(2026, 7, 15)), "NOPE": None})

    result = asyncio.run(market_data_service.coverage_for(["AAPL", "NOPE"]))

    assert result.missing == ["NOPE"]
    assert result.start is None and result.end is None
    # The good ticker is still reported: the caller needs to see which one is
    # at fault, not lose both.
    reported = {item.ticker: item for item in result.tickers}
    assert reported["AAPL"].last_bar == "2026-07-15"
    assert reported["NOPE"].first_bar is None


def test_one_ticker_is_its_own_window(stub_repo) -> None:
    stub_repo({"AAPL": (date(2019, 11, 11), date(2026, 7, 15))})

    result = asyncio.run(market_data_service.coverage_for(["AAPL"]))

    assert (result.start, result.end) == ("2019-11-11", "2026-07-15")


def test_dates_are_iso_strings_not_date_objects(stub_repo) -> None:
    """The client parses these with Zod; a date object would serialise wrong."""
    stub_repo({"AAPL": (date(2020, 1, 2), date(2026, 7, 15))})

    result = asyncio.run(market_data_service.coverage_for(["AAPL"]))

    assert isinstance(result.start, str)
    assert isinstance(result.tickers[0].last_bar, str)


# ---------------------------------------------------------------------------
# The wire contract and the route's own validation
# ---------------------------------------------------------------------------


def test_coverage_keys_are_camel_case() -> None:
    assert _aliases(CoverageResponse) == {"tickers", "start", "end", "missing"}
    assert _aliases(TickerCoverage) == {"ticker", "firstBar", "lastBar"}


def test_passing_neither_argument_is_422(client: TestClient) -> None:
    response = client.get("/api/market-data/coverage")
    assert response.status_code == 422
    assert "exactly one" in response.json()["detail"]


def test_passing_both_arguments_is_422(client: TestClient) -> None:
    """Ambiguous rather than harmless: the two could name different universes."""
    response = client.get(
        "/api/market-data/coverage", params={"tickers": "AAPL", "strategyKey": "x"}
    )
    assert response.status_code == 422


def test_an_empty_ticker_list_is_422(client: TestClient) -> None:
    response = client.get("/api/market-data/coverage", params={"tickers": " , ,"})
    assert response.status_code == 422
    assert "No tickers" in response.json()["detail"]
