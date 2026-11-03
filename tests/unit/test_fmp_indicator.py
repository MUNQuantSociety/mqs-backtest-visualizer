"""FMP technical-indicator series and the FmpIndicator that strategies register."""

import importlib
import io
import json
from datetime import date, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from engine.data import fmp
from engine.indicators import fmp_indicator
from engine.indicators.fmp_indicator import FmpIndicator, completed_session, load_series
from engine.strategies.portfolio_BASE.strategy import _camel_to_snake
from src.services.strategy_validation.scanning import (
    indicator_parameters,
    indicator_sources,
    known_indicators,
)


@pytest.fixture(autouse=True)
def isolated_fmp(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp.time, "sleep", lambda _: None)


def respond(monkeypatch, payload):
    calls = []

    def open_url(url, *, timeout):
        parsed = urlparse(url)
        calls.append((parsed.path, parse_qs(parsed.query)))
        return io.StringIO(json.dumps(payload))

    monkeypatch.setattr(fmp, "urlopen", open_url)
    return calls


def row(day, field, value):
    return {"date": f"{day} 00:00:00", "open": 1, "high": 1, "low": 1, "close": 1,
            "volume": 1, field: value}


# --- FMPMarketData.get_technical_indicator -----------------------------------

@pytest.mark.parametrize("name,field", sorted(fmp.TECHNICAL_INDICATORS.items()))
def test_every_indicator_reads_its_own_field(monkeypatch, name, field):
    calls = respond(monkeypatch, [row("2026-09-30", field, 2.5), row("2026-09-29", field, 1.5)])

    series = fmp.FMPMarketData().get_technical_indicator(
        "AAPL", name, 14, date(2026, 9, 1), date(2026, 9, 30)
    )

    assert series == [(date(2026, 9, 29), 1.5), (date(2026, 9, 30), 2.5)]
    path, query = calls[0]
    assert path == f"/stable/technical-indicators/{name}"
    assert query["symbol"] == ["AAPL"]
    assert query["periodLength"] == ["14"]
    assert query["timeframe"] == ["1day"]
    assert query["from"] == ["2026-09-01"] and query["to"] == ["2026-09-30"]


def test_empty_values_are_skipped_and_out_of_window_rows_dropped(monkeypatch):
    respond(monkeypatch, [row("2026-10-01", "rsi", 70), row("2026-09-30", "rsi", None),
                          row("2026-09-29", "rsi", 40), row("2026-08-31", "rsi", 10)])

    series = fmp.FMPMarketData().get_technical_indicator(
        "AAPL", "rsi", 14, date(2026, 9, 1), date(2026, 9, 30)
    )

    assert series == [(date(2026, 9, 29), 40.0)]


@pytest.mark.parametrize("name,period,start,end", [
    ("macd", 14, date(2026, 9, 1), date(2026, 9, 30)),
    ("rsi", 1, date(2026, 9, 1), date(2026, 9, 30)),
    ("rsi", 251, date(2026, 9, 1), date(2026, 9, 30)),
    ("rsi", 14, date(2026, 9, 30), date(2026, 9, 1)),
])
def test_bad_arguments_fail_before_any_request(monkeypatch, name, period, start, end):
    calls = respond(monkeypatch, [])

    with pytest.raises(ValueError):
        fmp.FMPMarketData().get_technical_indicator("AAPL", name, period, start, end)
    assert calls == []


@pytest.mark.parametrize("value", ["not-a-number", "nan", "inf"])
def test_garbage_values_are_a_provider_failure(monkeypatch, value):
    respond(monkeypatch, [row("2026-09-30", "sma", value)])

    with pytest.raises(fmp.FMPUnavailable, match="invalid sma"):
        fmp.FMPMarketData().get_technical_indicator(
            "AAPL", "sma", 20, date(2026, 9, 1), date(2026, 9, 30)
        )


def test_a_possibly_truncated_answer_is_refused(monkeypatch):
    first = date(2020, 1, 1)
    respond(monkeypatch, [row(first + timedelta(days=i), "ema", 1.0)
                          for i in range(fmp.TECHNICAL_INDICATOR_MAX_ROWS)])

    with pytest.raises(fmp.FMPUnavailable, match="truncated"):
        fmp.FMPMarketData().get_technical_indicator(
            "AAPL", "ema", 20, date(2020, 1, 1), date(2026, 1, 1)
        )


def test_plan_errors_keep_their_advice_and_never_show_the_key(monkeypatch):
    def refuse(url, *, timeout):
        raise HTTPError(url, 402, "Payment Required", {}, io.BytesIO(b""))

    monkeypatch.setattr(fmp, "urlopen", refuse)

    with pytest.raises(fmp.FMPUnavailable) as error:
        fmp.FMPMarketData().get_technical_indicator(
            "AAPL", "adx", 14, date(2026, 9, 1), date(2026, 9, 30)
        )
    assert "HTTP 402" in str(error.value) and "plan" in str(error.value)
    assert "test-secret-key" not in str(error.value)


# --- which session a bar may read -------------------------------------------

@pytest.mark.parametrize("stamp,expected", [
    (pd.Timestamp("2026-09-30 16:00", tz="America/New_York"), date(2026, 9, 30)),
    (pd.Timestamp("2026-09-30 10:00", tz="America/New_York"), date(2026, 9, 29)),
    (pd.Timestamp("2026-09-30 00:00", tz="America/New_York"), date(2026, 9, 29)),
    (pd.Timestamp("2026-09-30 20:00", tz="UTC"), date(2026, 9, 30)),
    (datetime(2026, 9, 30, 16, 0), date(2026, 9, 30)),
])
def test_a_bar_never_reads_a_close_that_has_not_happened(stamp, expected):
    assert completed_session(stamp) == expected


# --- chunked, warmed-up loading ---------------------------------------------

class FakeClient:
    def __init__(self, values=None):
        self.values = values or {}
        self.requests = []

    def get_technical_indicator(self, ticker, name, period, start, end):
        self.requests.append((ticker, name, period, start, end))
        return sorted((day, value) for day, value in self.values.items() if start <= day <= end)


def test_long_spans_are_split_and_every_chunk_starts_with_a_warmup():
    first, last = date(2018, 1, 1), date(2024, 12, 31)
    client = FakeClient({first + timedelta(days=i): float(i) for i in range(-800, 2600)})

    values = load_series(client, "AAPL", "rsi", 14, first, last)

    warmup = timedelta(days=fmp_indicator.warmup_days(14))
    starts = [first + timedelta(days=fmp_indicator.CHUNK_DAYS * i) for i in range(3)]
    assert [request[3] for request in client.requests] == [start - warmup for start in starts]
    assert client.requests[-1][4] == last
    assert min(values) == first and max(values) == last
    assert values[first] == 0.0


def test_requests_stay_inside_fmps_row_limit():
    span = fmp_indicator.CHUNK_DAYS + fmp_indicator.warmup_days(fmp.TECHNICAL_INDICATOR_MAX_PERIOD)
    trading_days = span * 252 / 365
    assert trading_days < fmp.TECHNICAL_INDICATOR_MAX_ROWS


# --- FmpIndicator ------------------------------------------------------------

def make_indicator(monkeypatch, values, **kwargs):
    client = FakeClient(values)
    monkeypatch.setattr(fmp_indicator, "FMPMarketData", lambda: client)
    return FmpIndicator("AAPL", **{"name": "rsi", "period": 14, **kwargs}), client


def close(day: str):
    return pd.Timestamp(f"{day} 16:00", tz="America/New_York")


def test_reads_fmps_value_for_the_bar_and_downloads_once(monkeypatch):
    indicator, client = make_indicator(monkeypatch, {
        date(2026, 9, 28): 41.0, date(2026, 9, 29): 50.9, date(2026, 9, 30): 54.4,
    })

    assert not indicator.IsReady
    indicator.Update(close("2026-09-29"), 999.0)
    assert indicator.IsReady and indicator.Current == 50.9
    indicator.Update(close("2026-09-30"), 999.0)
    assert indicator.Current == 54.4
    assert len(client.requests) == 1


def test_an_intraday_bar_reads_the_previous_close(monkeypatch):
    indicator, _ = make_indicator(monkeypatch, {date(2026, 9, 29): 50.9, date(2026, 9, 30): 54.4})

    indicator.Update(pd.Timestamp("2026-09-30 11:00", tz="America/New_York"), 1.0)

    assert indicator.Current == 50.9


def test_not_ready_before_the_first_value_or_after_a_long_gap(monkeypatch):
    indicator, _ = make_indicator(monkeypatch, {date(2026, 9, 1): 30.0})

    indicator.Update(close("2026-08-31"), 1.0)
    assert not indicator.IsReady and indicator.Current is None

    indicator.Update(close("2026-09-04"), 1.0)  # Friday, value from Tuesday
    assert indicator.IsReady and indicator.Current == 30.0

    indicator.Update(close("2026-09-15"), 1.0)  # 14 days stale
    assert not indicator.IsReady and indicator.Current is None


def test_a_session_past_the_download_triggers_one_more_fetch(monkeypatch):
    indicator, client = make_indicator(monkeypatch, {date(2026, 9, 29): 1.0})
    indicator._loaded = (date(2026, 9, 1), date(2026, 9, 29))
    indicator._values = {date(2026, 9, 29): 1.0}
    indicator._days = [date(2026, 9, 29)]
    client.values[date(2026, 9, 30)] = 2.0

    indicator.Update(close("2026-09-30"), 1.0)

    assert indicator.Current == 2.0
    assert client.requests[-1][3] <= date(2026, 9, 30) <= client.requests[-1][4]


@pytest.mark.parametrize("kwargs", [{"name": "macd"}, {"period": 1}, {"period": 300}])
def test_bad_settings_fail_when_the_strategy_is_built(monkeypatch, kwargs):
    with pytest.raises(ValueError):
        make_indicator(monkeypatch, {}, **kwargs)


def test_a_missing_api_key_fails_on_the_first_update(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "")
    monkeypatch.setattr(fmp, "load_dotenv", lambda *args, **kwargs: None)
    indicator = FmpIndicator("AAPL", name="sma", period=20)

    with pytest.raises(fmp.FMPUnavailable, match="FMP_API_KEY"):
        indicator.Update(close("2026-09-30"), 1.0)


# --- discoverable the way every engine indicator is --------------------------

def test_the_engine_loads_it_by_class_name():
    module = importlib.import_module(f"engine.indicators.{_camel_to_snake('FmpIndicator')}")
    assert module.FmpIndicator is FmpIndicator


def test_strategies_and_the_catalogue_see_exactly_one_new_indicator():
    assert "FmpIndicator" in known_indicators()
    module_classes = [name for name, source in indicator_sources().items()
                      if "class FmpIndicator" in source]
    assert module_classes == ["FmpIndicator"]
    params = dict(indicator_parameters(indicator_sources()["FmpIndicator"]))
    assert params["name"] == "sma" and params["period"] == 14
