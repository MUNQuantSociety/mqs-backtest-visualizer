"""Intraday bars: sizes, close labelling, both sources, and the engine gates."""

import io
import json
import math
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.contracts import NoMarketData
from engine.contracts.errors import EngineError
from engine.data import bar_interval, fmp, intraday
from engine.data.bar_interval import (
    IntradayWindowTooLarge,
    bar_minutes,
    check_intraday_size,
    estimate_bar_count,
    is_intraday,
    warmup_calendar_days,
    weekdays_between,
)
from engine.data.intraday import (
    IntradayResolutionUnavailable,
    fetch_db_intraday_bars,
    fetch_intraday_bars,
    fmp_chunks,
    label_bar_close,
    parse_fmp_intraday_rows,
)
from engine.run_single import _reject_unsupported_bar_interval

NY = ZoneInfo("America/New_York")


@pytest.fixture
def fmp_source(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp.time, "sleep", lambda _: None)


def fmp_bar(start, price=100.0, volume=1000):
    return {"date": start, "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": volume}


def serve_fmp(monkeypatch, rows_for):
    """Answer each FMP request with ``rows_for(path, query)``; return the calls."""
    calls = []

    def open_url(url, *, timeout):
        parsed = urlparse(url)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        calls.append((parsed.path, query))
        return io.StringIO(json.dumps(rows_for(parsed.path, query)))

    monkeypatch.setattr(fmp, "urlopen", open_url)
    return calls


class FakeDB:
    """Answers the spacing probe and bucket queries the DB path issues."""

    def __init__(self, stored_minutes, bucket_rows=()):
        self.stored_minutes = stored_minutes
        self.bucket_rows = list(bucket_rows)
        self.queries = []

    def execute_query(self, sql, params, fetch=True):
        self.queries.append((sql, params))
        if "gap_minutes" in sql:
            data = [] if self.stored_minutes is None else [
                {"gap_minutes": float(self.stored_minutes), "occurrences": 10}
            ]
            return {"status": "success", "data": data}
        return {"status": "success", "data": self.bucket_rows}


# --- bar sizes ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(60, 1), (300, 5), (900, 15), (1800, 30), (3600, 60), (86400, 1440)],
)
def test_bar_minutes_converts_each_supported_size(seconds, minutes):
    assert bar_minutes(seconds) == minutes


@pytest.mark.parametrize("value", [0, 59, 90, 120, 7200, 86401, -60, True, "60", None, math.nan])
def test_bar_minutes_rejects_unsupported_values(value):
    with pytest.raises(ValueError, match="Bar interval must be one of"):
        bar_minutes(value)


def test_daily_bar_is_not_intraday():
    assert is_intraday(86400) is False


def test_one_hour_bar_is_intraday():
    assert is_intraday(3600) is True


# --- labelling -----------------------------------------------------------------


def test_bar_is_labelled_at_its_close_not_its_start():
    starts = pd.Series(pd.to_datetime(["2026-08-03 09:30"]).tz_localize(NY))
    assert label_bar_close(starts, 5).iloc[0] == pd.Timestamp("2026-08-03 09:35", tz=NY)


def test_last_short_bar_is_capped_at_the_session_close():
    starts = pd.Series(pd.to_datetime(["2026-08-03 15:30"]).tz_localize(NY))
    assert label_bar_close(starts, 60).iloc[0] == pd.Timestamp("2026-08-03 16:00", tz=NY)


# --- size limits ---------------------------------------------------------------


def test_estimate_counts_weekdays_times_bars_times_tickers():
    # 2026-08-03 is a Monday: one full week of 1-minute bars for two tickers.
    assert estimate_bar_count(2, date(2026, 8, 3), date(2026, 8, 9), 1) == 2 * 5 * 390


def test_estimate_is_zero_for_an_inverted_window():
    assert estimate_bar_count(3, date(2026, 8, 9), date(2026, 8, 3), 1) == 0


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 8, 8), date(2026, 8, 9), 0),  # Saturday..Sunday
        (date(2026, 8, 7), date(2026, 8, 10), 2),  # Friday..Monday
        (date(2026, 8, 3), date(2026, 8, 30), 20),  # four full weeks
        (date(2026, 8, 5), date(2026, 8, 5), 1),  # one Wednesday
    ],
)
def test_weekdays_between_counts_monday_to_friday(start, end, expected):
    assert weekdays_between(start, end) == expected


def test_hourly_session_counts_its_short_last_bar():
    assert estimate_bar_count(1, date(2026, 8, 3), date(2026, 8, 3), 60) == 7


def test_window_at_the_limit_is_accepted(monkeypatch):
    monkeypatch.setattr(bar_interval, "MAX_INTRADAY_BARS", 390)
    check_intraday_size(1, date(2026, 8, 3), date(2026, 8, 3), 1)


def test_window_one_session_past_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(bar_interval, "MAX_INTRADAY_BARS", 390)
    with pytest.raises(IntradayWindowTooLarge, match="Choose a larger bar interval"):
        check_intraday_size(1, date(2026, 8, 3), date(2026, 8, 4), 1)


def test_warmup_covers_period_of_one_minute_bars_within_a_week():
    assert warmup_calendar_days(20, 1) == 6


def test_warmup_grows_with_bars_needed_across_sessions():
    # 200 hourly bars need 29 sessions.
    assert warmup_calendar_days(200, 60) == math.ceil(29 * 7 / 5 * 1.2) + 4


# --- FMP source ----------------------------------------------------------------


def test_fmp_chunks_cover_the_window_without_gaps_or_overlap():
    chunks = fmp_chunks(date(2026, 8, 1), date(2026, 8, 10), 1)
    assert chunks[0][0] == date(2026, 8, 1) and chunks[-1][1] == date(2026, 8, 10)
    for (_, previous_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert (next_start - previous_end).days == 1
    assert all((end - start).days + 1 <= 3 for start, end in chunks)


def test_fmp_rows_become_close_labelled_session_bars():
    frame = parse_fmp_intraday_rows(
        "AAPL",
        [fmp_bar("2026-08-03 09:35:00", 101), fmp_bar("2026-08-03 09:30:00", 100)],
        5,
    )
    assert list(frame["timestamp"]) == [
        pd.Timestamp("2026-08-03 09:35", tz=NY),
        pd.Timestamp("2026-08-03 09:40", tz=NY),
    ]
    assert list(frame["close_price"]) == [100.0, 101.0]


def test_fmp_rows_outside_regular_hours_are_dropped():
    frame = parse_fmp_intraday_rows(
        "AAPL", [fmp_bar("2026-08-03 09:29:00"), fmp_bar("2026-08-03 16:00:00")], 1
    )
    assert frame.empty


@pytest.mark.parametrize(
    "row",
    [fmp_bar("2026-08-03 09:30:00", price=0), fmp_bar("not-a-date"),
     {"date": "2026-08-03 09:30:00", "open": 1}],
)
def test_malformed_fmp_row_is_a_provider_failure(row):
    with pytest.raises(fmp.FMPUnavailable, match="invalid intraday bar"):
        parse_fmp_intraday_rows("AAPL", [row], 1)


def test_fmp_client_refuses_an_unknown_interval(fmp_source):
    with pytest.raises(ValueError, match="Unsupported FMP intraday interval"):
        fmp.FMPMarketData().get_intraday_history(
            "AAPL", "2hour", date(2026, 8, 3), date(2026, 8, 3)
        )


def test_fmp_window_is_requested_in_interval_sized_chunks(fmp_source, monkeypatch):
    calls = serve_fmp(monkeypatch, lambda path, query: [fmp_bar(f"{query['from']} 09:30:00")])
    frame = fetch_intraday_bars(None, ["aapl"], date(2026, 8, 3), date(2026, 8, 9), 1)
    assert {path for path, _ in calls} == {"/stable/historical-chart/1min"}
    assert sorted((q["from"], q["to"]) for _, q in calls) == [
        ("2026-08-03", "2026-08-05"), ("2026-08-06", "2026-08-08"), ("2026-08-09", "2026-08-09"),
    ]
    assert set(frame["ticker"]) == {"AAPL"}


def test_ticker_without_intraday_bars_fails_the_run(fmp_source, monkeypatch):
    serve_fmp(monkeypatch, lambda path, query: (
        [fmp_bar("2026-08-03 10:00:00")] if query["symbol"] == "AAPL" else []
    ))
    with pytest.raises(NoMarketData, match="MSFT"):
        fetch_intraday_bars(None, ["AAPL", "MSFT"], date(2026, 8, 3), date(2026, 8, 3), 5)


def test_run_adapter_downloads_an_overlapping_window_once(fmp_source, monkeypatch):
    calls = serve_fmp(monkeypatch, lambda path, query: [fmp_bar("2026-08-04 10:00:00")])
    adapter = fmp.FMPDataAdapter()
    adapter.get_intraday_history(["AAPL"], date(2026, 8, 3), date(2026, 8, 5), 60)
    first_count = len(calls)
    frame = adapter.get_intraday_history(["AAPL"], date(2026, 8, 4), date(2026, 8, 4), 60)
    assert len(calls) == first_count
    assert list(frame["timestamp"]) == [pd.Timestamp("2026-08-04 11:00", tz=NY)]


# --- database source -----------------------------------------------------------


def test_db_refuses_bars_finer_than_the_stored_rows():
    with pytest.raises(IntradayResolutionUnavailable, match="stores 60-minute bars"):
        fetch_db_intraday_bars(FakeDB(60), ["AAPL"], date(2026, 8, 3), date(2026, 8, 3), 5)


def test_db_refuses_a_size_that_is_not_a_multiple_of_the_stored_rows():
    with pytest.raises(IntradayResolutionUnavailable):
        fetch_db_intraday_bars(FakeDB(60), ["AAPL"], date(2026, 8, 3), date(2026, 8, 3), 90)


def test_db_buckets_are_close_labelled_from_the_session_open():
    rows = [{"ticker": "AAPL", "trade_date": date(2026, 8, 3), "bucket_index": 2.0,
             "open_price": 1, "high_price": 2, "low_price": 0.5, "close_price": 1.5,
             "volume": 10}]
    frame = fetch_db_intraday_bars(
        FakeDB(1, rows), ["AAPL"], date(2026, 8, 3), date(2026, 8, 3), 15
    )
    # Bucket 2 of 15 minutes starts at 10:00 and closes at 10:15.
    assert list(frame["timestamp"]) == [pd.Timestamp("2026-08-03 10:15", tz=NY)]


def test_db_bucket_width_is_a_bound_parameter_not_sql_text():
    db = FakeDB(1)
    fetch_db_intraday_bars(db, ["AAPL"], date(2026, 8, 3), date(2026, 8, 3), 15)
    sql, params = db.queries[-1]
    assert params[0] == 15 and "15" not in sql


def test_db_query_failure_is_an_outage_not_an_empty_window():
    class BrokenDB:
        def execute_query(self, sql, params, fetch=True):
            return {"status": "error", "message": "timeout"}

    with pytest.raises(intraday.MarketDataUnavailable):
        fetch_db_intraday_bars(BrokenDB(), ["AAPL"], date(2026, 8, 3), date(2026, 8, 3), 60)


def test_db_window_is_queried_in_slabs(monkeypatch):
    monkeypatch.setattr(intraday, "_DB_SLAB_DAYS", 2)
    db = FakeDB(1)
    fetch_db_intraday_bars(db, ["AAPL"], date(2026, 8, 3), date(2026, 8, 7), 5)
    bucket_queries = [params for sql, params in db.queries if "bucket_index" in sql]
    assert [(p[2].date(), p[3].date()) for p in bucket_queries] == [
        (date(2026, 8, 3), date(2026, 8, 5)),
        (date(2026, 8, 5), date(2026, 8, 7)),
        (date(2026, 8, 7), date(2026, 8, 8)),
    ]


# --- engine gates --------------------------------------------------------------


def test_run_without_a_bar_interval_stays_daily():
    _reject_unsupported_bar_interval({}, "fast")


def test_fast_mode_refuses_intraday_bars():
    with pytest.raises(EngineError, match="Fast mode"):
        _reject_unsupported_bar_interval({"BAR_INTERVAL_SECONDS": 300}, "fast")


def test_unsupported_bar_interval_fails_the_run():
    with pytest.raises(EngineError, match="Bar interval must be one of"):
        _reject_unsupported_bar_interval({"BAR_INTERVAL_SECONDS": 120}, "event")


def test_session_bounds_are_half_open_new_york_midnights():
    lower, upper = intraday._session_bounds(date(2026, 8, 3), date(2026, 8, 3))
    assert lower == datetime(2026, 8, 3, tzinfo=NY)
    assert upper == datetime(2026, 8, 4, tzinfo=NY)


# --- whole run -----------------------------------------------------------------


def test_hourly_run_decides_on_every_bar_including_the_short_closing_bar(
    fmp_source, monkeypatch, tmp_path
):
    from engine.contracts import RunRequest
    from engine.run_single import run_single

    hour_starts = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]

    def rows_for(path, query):
        days = pd.bdate_range(query["from"], query["to"])
        if path.endswith("/historical-price-eod/full"):
            # The engine still reads daily closes for its benchmark setup.
            return [{**fmp_bar(str(day.date())), "symbol": query["symbol"]} for day in days][::-1]
        return [fmp_bar(f"{day.date()} {start}:00", 100 + index)
                for day in days for index, start in enumerate(hour_starts)][::-1]

    serve_fmp(monkeypatch, rows_for)
    monkeypatch.setattr("engine.run_single.EngineDBAdapter", lambda *a, **k: pytest.fail("DB used"))
    monkeypatch.setattr("engine.data.cache.load", lambda *a, **k: pytest.fail("daily cache used"))
    result = run_single(RunRequest(
        run_id="hourly", strategy_key="portfolio_1",
        class_path="engine.strategies.portfolio_1.strategy:VolMomentum",
        start_date="2025-03-31", end_date="2025-04-01", initial_capital=10_000, mode="event",
        params={"TICKERS": ["AAPL"], "WEIGHTS": {"AAPL": 1.0}, "LOOKBACK_DAYS": 1,
                "BAR_INTERVAL_SECONDS": 3600, "INTERVAL": 0},
        artifact_dir=str(tmp_path),
    ))
    assert result.status == "completed", result.error
    # Two sessions of seven hourly bars plus the capital baseline. The first
    # day's 15:30-16:00 bar is the one a per-hour decision cadence would skip;
    # the last day's is recorded regardless, so one day would not show it.
    assert len(result.equity_curve) == 2 * 7 + 1


# --- open items: late starts, spacing samples, early size check, daily warmup --


def _hourly_rows(first_day_for=None):
    """FMP mock serving 09:30-15:30 hourly bars, and daily bars for the benchmark.

    ``first_day_for`` maps a ticker to the first date FMP has intraday bars for.
    """
    first_day_for = first_day_for or {}
    hour_starts = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]

    def rows_for(path, query):
        days = pd.bdate_range(query["from"], query["to"])
        if path.endswith("/historical-price-eod/full"):
            return [{**fmp_bar(str(day.date())), "symbol": query["symbol"]} for day in days][::-1]
        first = pd.Timestamp(first_day_for.get(query["symbol"], "1900-01-01"))
        return [fmp_bar(f"{day.date()} {start}:00", 100 + index)
                for day in days if day >= first
                for index, start in enumerate(hour_starts)][::-1]

    return rows_for


def _vol_momentum_request(tmp_path, *, tickers, start, end, bar_seconds):
    from engine.contracts import RunRequest

    return RunRequest(
        run_id="open-items", strategy_key="portfolio_1",
        class_path="engine.strategies.portfolio_1.strategy:VolMomentum",
        start_date=start, end_date=end, initial_capital=10_000, mode="event",
        params={"TICKERS": tickers, "WEIGHTS": {t: 1 / len(tickers) for t in tickers},
                "LOOKBACK_DAYS": 1, "BAR_INTERVAL_SECONDS": bar_seconds, "INTERVAL": 0},
        artifact_dir=str(tmp_path),
    )


def test_late_starting_tickers_names_each_ticker_and_its_first_day():
    frame = pd.DataFrame({
        "ticker": ["AAPL", "MSFT"],
        "timestamp": [
            pd.Timestamp("2026-08-03 10:00", tz=NY), pd.Timestamp("2026-08-12 10:00", tz=NY),
        ],
    })
    assert intraday.late_starting_tickers(frame, ["AAPL", "MSFT"], date(2026, 8, 3)) == {
        "MSFT": date(2026, 8, 12)
    }


def test_first_bar_after_a_long_weekend_is_not_late():
    # Saturday start; Monday 2026-09-07 is Labor Day, so Tuesday is the first session.
    frame = pd.DataFrame(
        {"ticker": ["AAPL"], "timestamp": [pd.Timestamp("2026-09-08 10:30", tz=NY)]}
    )
    assert intraday.late_starting_tickers(frame, ["AAPL"], date(2026, 9, 5)) == {}


def test_bars_before_the_window_do_not_count_as_coverage():
    # A lookback bar must not hide that the simulated window itself starts late.
    frame = pd.DataFrame({
        "ticker": ["AAPL", "AAPL"],
        "timestamp": [
            pd.Timestamp("2026-07-20 10:00", tz=NY), pd.Timestamp("2026-08-12 10:00", tz=NY),
        ],
    })
    assert intraday.late_starting_tickers(frame, ["AAPL"], date(2026, 8, 3)) == {
        "AAPL": date(2026, 8, 12)
    }


def test_run_whose_intraday_history_starts_late_fails_and_says_where(
    fmp_source, monkeypatch, tmp_path
):
    from engine.run_single import run_single

    serve_fmp(monkeypatch, _hourly_rows({"MSFT": "2025-03-31"}))
    result = run_single(_vol_momentum_request(
        tmp_path, tickers=["AAPL", "MSFT"], start="2025-03-24", end="2025-04-01", bar_seconds=3600,
    ))
    assert result.status == "failed"
    assert "MSFT" in result.error and "2025-03-31" in result.error


def test_oversized_intraday_run_fails_before_any_download(fmp_source, monkeypatch, tmp_path):
    from engine.run_single import run_single

    calls = serve_fmp(monkeypatch, _hourly_rows())
    result = run_single(_vol_momentum_request(
        tmp_path, tickers=["AAPL", "MSFT", "NVDA", "AMD", "TSLA"],
        start="2025-01-02", end="2025-12-31", bar_seconds=60,
    ))
    assert result.status == "failed" and "would load about" in result.error
    assert calls == []


class WindowedSpacingDB(FakeDB):
    """Stores 1-minute rows before ``coarse_from`` and hourly rows from it on."""

    def __init__(self, coarse_from):
        super().__init__(stored_minutes=1)
        self.coarse_from = coarse_from

    def execute_query(self, sql, params, fetch=True):
        if "gap_minutes" in sql:
            self.queries.append((sql, params))
            window_start = params[-2].date()
            gap = 60.0 if window_start >= self.coarse_from else 1.0
            return {"status": "success", "data": [{"gap_minutes": gap, "occurrences": 10}]}
        return super().execute_query(sql, params, fetch)


def test_spacing_probe_samples_the_start_middle_and_end_of_the_window():
    windows = intraday.spacing_probe_windows(date(2026, 1, 1), date(2026, 12, 31))
    assert windows[0][0] == date(2026, 1, 1)
    assert windows[-1][1] == date(2026, 12, 31)
    assert any(date(2026, 5, 1) <= start <= date(2026, 8, 31) for start, _ in windows)


def test_short_window_is_probed_once():
    assert intraday.spacing_probe_windows(date(2026, 8, 3), date(2026, 8, 5)) == [
        (date(2026, 8, 3), date(2026, 8, 5))
    ]


def test_db_refuses_a_window_whose_later_rows_are_coarser():
    db = WindowedSpacingDB(coarse_from=date(2026, 6, 1))
    with pytest.raises(IntradayResolutionUnavailable, match="60-minute"):
        fetch_db_intraday_bars(db, ["AAPL"], date(2026, 1, 5), date(2026, 8, 28), 5)


def test_daily_database_warmup_reads_daily_bars_not_raw_rows(monkeypatch):
    from engine.strategies.portfolio_1.strategy import VolMomentum

    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    calls = []

    def daily_bars(portfolio, tickers, start, end):
        calls.append((list(tickers), start, end))
        return pd.DataFrame({
            "ticker": ["AAPL"], "timestamp": [pd.Timestamp("2025-03-27 16:00", tz=NY)],
            "open_price": [100.0], "high_price": [101.0], "low_price": [99.0],
            "close_price": [100.0], "volume": [1000.0],
        })

    class RawRowsForbidden:
        def execute_query(self, *args, **kwargs):
            pytest.fail("daily warmup read raw market_data rows")

    monkeypatch.setattr("engine.core.utils._fetch_from_db", daily_bars)
    VolMomentum(
        db_connector=RawRowsForbidden(), executor=None,
        config_dict={"TICKERS": ["AAPL"], "LOOKBACK_DAYS": 30},
        backtest_start_date=datetime(2025, 3, 28),
    )
    assert calls and calls[0][0] == ["AAPL"]
    # Warmup ends before the first simulated day, as on the FMP path.
    assert calls[0][2] == datetime(2025, 3, 28, tzinfo=NY)
