"""A benchmark ticker's daily closes, for the dashboard's benchmark line.

The dashboard rebases runs and a benchmark to 100 over the same dates; SPY is
not traded by most runs, so its closes are read directly from market data.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from engine.data.fmp import FMPUnavailable

from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_data as market_data_api
from src.services import market_data


@pytest.fixture
def stub_session(monkeypatch):
    @asynccontextmanager
    async def fake_scope():
        yield object()

    monkeypatch.setattr(market_data, "session_scope", fake_scope)
    monkeypatch.setattr(market_data.market_data_repo, "limit_statement_time", _noop)


async def _noop(*_args, **_kwargs):
    return None


def _stub_closes(monkeypatch, rows):
    calls = []

    async def closes(_session, window_by_ticker):
        calls.append(dict(window_by_ticker))
        return rows

    monkeypatch.setattr(market_data.market_data_repo, "daily_closes", closes)
    return calls


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_returns_the_tickers_closes_oldest_first(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {"SPY": [(date(2026, 3, 2), 510.5), (date(2026, 3, 3), 512.0)]})

    result = asyncio.run(market_data.closes_between("spy", date(2026, 3, 1), date(2026, 3, 31)))

    assert result.ticker == "SPY"
    assert [(point.date, point.close) for point in result.points] == [
        ("2026-03-02", 510.5),
        ("2026-03-03", 512.0),
    ]


def test_reads_from_the_start_of_the_first_new_york_day_to_the_last(monkeypatch, stub_session, fmp):
    calls = _stub_closes(monkeypatch, {})

    asyncio.run(market_data.closes_between("SPY", date(2026, 3, 1), date(2026, 3, 31)))

    since, until = calls[0]["SPY"]
    assert since == datetime(2026, 3, 1, tzinfo=market_data._EXCHANGE_TZ)
    assert until == date(2026, 3, 31)


def test_a_ticker_with_no_closes_in_range_has_no_points(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {})

    result = asyncio.run(market_data.closes_between("SPY", date(2026, 3, 1), date(2026, 3, 31)))

    assert result.points == []


@pytest.mark.parametrize(
    ("ticker", "start", "end"),
    [
        ("SPY", date(2026, 3, 31), date(2026, 3, 1)),  # start after end
        ("", date(2026, 3, 1), date(2026, 3, 31)),
        ("SPY; DROP", date(2026, 3, 1), date(2026, 3, 31)),
        ("SPY", date(2000, 1, 1), date(2026, 3, 31)),  # more than the span cap
    ],
)
def test_refuses_an_invalid_request(ticker, start, end):
    with pytest.raises(ValueError):
        asyncio.run(market_data.closes_between(ticker, start, end))


def _fmp_frame(rows):
    """What fetch_daily_history returns: daily bars labelled at 16:00 New York."""
    import pandas as pd

    if not rows:
        return pd.DataFrame()  # what fetch_daily_history returns with no bars
    frame = pd.DataFrame(
        [{"ticker": "SPY", "date": day, "close_price": close} for day, close in rows]
    )
    frame["timestamp"] = (pd.to_datetime(frame.pop("date")) + pd.Timedelta(hours=16)).dt.tz_localize(
        "America/New_York"
    )
    return frame


@pytest.fixture
def fmp(monkeypatch):
    """Records FMP calls and answers with the rows a test sets."""
    state = {"rows": [], "calls": [], "error": None}

    def fetch(tickers, start, end, *, require_all=True):
        state["calls"].append((tuple(tickers), start, end, require_all))
        if state["error"]:
            raise state["error"]
        return _fmp_frame(state["rows"])

    monkeypatch.setattr(market_data, "fetch_daily_history", fetch)
    market_data._closes_fill_cache.clear()
    return state


MARCH = [(date(2026, 3, day), 500.0 + day) for day in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]


def test_does_not_ask_fmp_when_the_database_covers_the_window(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {"SPY": MARCH})

    asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 13)))

    assert fmp["calls"] == []


def test_fills_a_window_the_database_has_nothing_for(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {})
    fmp["rows"] = [("2026-03-02", 510.0), ("2026-03-03", 511.0)]

    result = asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 3)))

    assert [(point.date, point.close) for point in result.points] == [
        ("2026-03-02", 510.0),
        ("2026-03-03", 511.0),
    ]


def test_fills_the_start_the_database_is_missing_and_keeps_its_own_closes(
    monkeypatch, stub_session, fmp
):
    # The database starts on the 9th; FMP has the whole window, slightly different.
    _stub_closes(monkeypatch, {"SPY": [(day, close) for day, close in MARCH if day.day >= 9]})
    fmp["rows"] = [(day.isoformat(), close - 1) for day, close in MARCH]

    result = asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 13)))

    closes = {point.date: point.close for point in result.points}
    assert len(closes) == len(MARCH)
    assert closes["2026-03-02"] == 501.0  # from FMP
    assert closes["2026-03-09"] == 509.0  # the database wins where it has the day
    assert [point.date for point in result.points] == sorted(closes)


def test_fills_a_hole_inside_the_window(monkeypatch, stub_session, fmp):
    held = [(day, close) for day, close in MARCH if day.day in (2, 3, 12, 13)]
    _stub_closes(monkeypatch, {"SPY": held})
    fmp["rows"] = [(day.isoformat(), close) for day, close in MARCH]

    result = asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 13)))

    assert len(result.points) == len(MARCH)


def test_returns_what_the_database_has_when_fmp_is_unavailable(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {"SPY": [(date(2026, 3, 13), 513.0)]})
    fmp["error"] = FMPUnavailable("provider down")

    result = asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 13)))

    assert [(point.date, point.close) for point in result.points] == [("2026-03-13", 513.0)]


def test_asks_fmp_once_for_a_repeated_window(monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {})
    fmp["rows"] = [("2026-03-02", 510.0)]

    for _ in range(2):
        asyncio.run(market_data.closes_between("SPY", date(2026, 3, 2), date(2026, 3, 13)))

    assert len(fmp["calls"]) == 1


def test_a_database_failure_is_reported_as_unavailable(monkeypatch, stub_session):
    async def failing(_session, _window):
        raise OperationalError("SELECT", {}, Exception("timeout"))

    monkeypatch.setattr(market_data.market_data_repo, "daily_closes", failing)

    with pytest.raises(market_data.MarketDataUnavailable):
        asyncio.run(market_data.closes_between("SPY", date(2026, 3, 1), date(2026, 3, 31)))


def test_endpoint_returns_camel_case_points(client, monkeypatch, stub_session, fmp):
    _stub_closes(monkeypatch, {"SPY": [(date(2026, 3, 2), 510.5)]})

    response = client.get(
        "/api/market-data/closes", params={"ticker": "SPY", "start": "2026-03-01", "end": "2026-03-31"}
    )

    assert response.status_code == 200
    assert response.json() == {"ticker": "SPY", "points": [{"date": "2026-03-02", "close": 510.5}]}


def test_endpoint_answers_422_for_an_invalid_request(client):
    response = client.get(
        "/api/market-data/closes", params={"ticker": "SPY", "start": "2026-03-31", "end": "2026-03-01"}
    )

    assert response.status_code == 422


def test_endpoint_answers_503_when_the_database_fails(client, monkeypatch, stub_session):
    async def failing(_session, _window):
        raise OperationalError("SELECT", {}, Exception("timeout"))

    monkeypatch.setattr(market_data.market_data_repo, "daily_closes", failing)

    response = client.get(
        "/api/market-data/closes", params={"ticker": "SPY", "start": "2026-03-01", "end": "2026-03-31"}
    )

    assert response.status_code == 503


def test_endpoint_requires_a_signed_in_user():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")

    response = TestClient(app).get(
        "/api/market-data/closes", params={"ticker": "SPY", "start": "2026-03-01", "end": "2026-03-31"}
    )

    assert response.status_code in {401, 403}
