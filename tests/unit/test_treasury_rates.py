"""The latest US Treasury yield curve, for the financial calculator's rates."""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.data import fmp as fmp_module
from engine.data.fmp import FMPMarketData, FMPUnavailable

from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_data as market_data_api
from src.services import market_data


def _curve(day, **rates):
    return {"date": day, **rates}


@pytest.fixture
def fmp(monkeypatch):
    """Records Treasury requests and answers with the curves a test sets."""
    state = {"curves": [], "calls": 0, "error": None}

    class FakeProvider:
        def get_treasury_rates(self, start, end):
            state["calls"] += 1
            if state["error"]:
                raise state["error"]
            return state["curves"]

    monkeypatch.setattr(market_data, "FMPMarketData", FakeProvider)
    monkeypatch.setattr(market_data, "_treasury_cache", None)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_data_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_returns_the_newest_curve_shortest_maturity_first(fmp):
    fmp["curves"] = [
        _curve(date(2026, 10, 28), month3=4.18, year10=5.05, year1=4.47),
        _curve(date(2026, 10, 27), month3=4.22, year10=4.99, year1=4.5),
    ]

    result = asyncio.run(market_data.treasury_rates())

    assert result.date == "2026-10-28"
    assert [(y.term, y.years, y.rate_percent) for y in result.yields] == [
        ("3M", 0.25, 4.18),
        ("1Y", 1, 4.47),
        ("10Y", 10, 5.05),
    ]


def test_skips_a_day_with_no_rates(fmp):
    fmp["curves"] = [_curve(date(2026, 10, 28)), _curve(date(2026, 10, 27), year10=4.99)]

    result = asyncio.run(market_data.treasury_rates())

    assert result.date == "2026-10-27"


def test_asks_fmp_once_an_hour(fmp):
    fmp["curves"] = [_curve(date(2026, 10, 28), year10=5.05)]

    for _ in range(3):
        asyncio.run(market_data.treasury_rates())

    assert fmp["calls"] == 1


def test_no_rates_at_all_is_unavailable(fmp):
    with pytest.raises(FMPUnavailable, match="no Treasury rates"):
        asyncio.run(market_data.treasury_rates())


def test_route_answers_in_camel_case(client, fmp):
    fmp["curves"] = [_curve(date(2026, 10, 28), month1=4.07, year30=5.37)]

    response = client.get("/api/market-data/treasury-rates")

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-10-28",
        "yields": [
            {"term": "1M", "years": pytest.approx(1 / 12), "ratePercent": 4.07},
            {"term": "30Y", "years": 30, "ratePercent": 5.37},
        ],
    }


def test_route_reports_a_provider_failure_as_503(client, fmp):
    fmp["error"] = FMPUnavailable("provider down")

    assert client.get("/api/market-data/treasury-rates").status_code == 503


def test_the_engine_drops_fields_it_cannot_read_and_sorts_newest_first(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    rows = [
        {"date": "2026-10-27", "year10": 4.99, "year30": None},
        {"date": "2026-10-28", "year10": 5.05, "year20": "n/a", "month1": True},
    ]
    monkeypatch.setattr(FMPMarketData, "_request_list", lambda self, *args, **kwargs: rows)

    curves = FMPMarketData().get_treasury_rates(date(2026, 10, 20), date(2026, 10, 28))

    assert curves == [
        {"date": date(2026, 10, 28), "year10": 5.05},
        {"date": date(2026, 10, 27), "year10": 4.99},
    ]


def test_the_engine_refuses_a_row_without_a_date(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(FMPMarketData, "_request_list", lambda self, *args, **kwargs: [{"year10": 5}])

    with pytest.raises(FMPUnavailable, match="invalid Treasury"):
        FMPMarketData().get_treasury_rates(date(2026, 10, 20), date(2026, 10, 28))


def test_every_maturity_has_a_label():
    assert [market_data._term(field) for field, _ in fmp_module.TREASURY_MATURITIES] == [
        "1M", "2M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y",
    ]
