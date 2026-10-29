"""The latest US Treasury yield curve, for the financial calculator's rates."""

from datetime import date

import pytest

from engine.data.fmp import FMPMarketData, FMPUnavailable


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
