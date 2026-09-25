"""The daily-closes SQL, executed by PostgreSQL itself on fixture bars.

``daily_closes`` walks each ticker's window one New York day at a time and
takes the last bar of the regular session. The unit tests for indicators and
benchmark closes stub the repository, so without this file no test runs the
query. Here only the table reference is swapped for a CTE built from fixture
rows: no table is created, read or written, so any reachable database will do.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.db.engine import create_sync_engine
from src.repositories import market_data as market_data_repo

pytestmark = pytest.mark.db

NY = ZoneInfo("America/New_York")


def _bar(ticker: str, day: str, clock: str, close: float | None) -> dict:
    moment = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=NY)
    return {"ticker": ticker, "timestamp": moment.isoformat(), "close_price": close}


class _FixtureSession:
    """Runs the production query with ``public.market_data`` replaced by fixture rows."""

    def __init__(self, connection, bars: list[dict]):
        self.connection = connection
        self.bars = bars

    async def execute(self, statement, params):
        sql = str(statement).replace("public.market_data", "fixture_bars")
        sql = (
            "WITH fixture_bars AS (SELECT * FROM jsonb_to_recordset(CAST(:fixture AS jsonb)) "
            'AS source(ticker text, "timestamp" timestamptz, close_price numeric)) ' + sql
        )
        return self.connection.execute(text(sql), {**params, "fixture": json.dumps(self.bars)})


def _closes(bars: list[dict], windows: dict[str, tuple[date, date]]):
    engine = create_sync_engine()
    try:
        with engine.connect() as connection:
            session = _FixtureSession(connection, bars)
            by_ticker = {
                ticker: (datetime.combine(start, datetime.min.time(), tzinfo=NY), end)
                for ticker, (start, end) in windows.items()
            }
            return asyncio.run(market_data_repo.daily_closes(session, by_ticker))
    finally:
        engine.dispose()


def test_takes_the_last_bar_of_each_session(require_database):
    bars = [
        _bar("SPY", "2026-03-02", "09:30:00", 500.0),
        _bar("SPY", "2026-03-02", "15:59:00", 501.0),
        _bar("SPY", "2026-03-02", "16:00:00", 502.0),
        _bar("SPY", "2026-03-03", "16:00:00", 510.0),
    ]

    closes = _closes(bars, {"SPY": (date(2026, 3, 2), date(2026, 3, 3))})

    assert closes == {"SPY": [(date(2026, 3, 2), 502.0), (date(2026, 3, 3), 510.0)]}


def test_ignores_bars_outside_regular_hours(require_database):
    bars = [
        _bar("SPY", "2026-03-02", "08:00:00", 490.0),  # pre-market
        _bar("SPY", "2026-03-02", "12:00:00", 500.0),
        _bar("SPY", "2026-03-02", "17:30:00", 520.0),  # after hours
    ]

    closes = _closes(bars, {"SPY": (date(2026, 3, 2), date(2026, 3, 2))})

    assert closes == {"SPY": [(date(2026, 3, 2), 500.0)]}


def test_a_day_with_no_bars_has_no_close(require_database):
    # Friday and Monday with bars; the weekend between has none.
    bars = [
        _bar("SPY", "2026-03-06", "16:00:00", 505.0),
        _bar("SPY", "2026-03-09", "16:00:00", 507.0),
    ]

    closes = _closes(bars, {"SPY": (date(2026, 3, 6), date(2026, 3, 9))})

    assert [day for day, _ in closes["SPY"]] == [date(2026, 3, 6), date(2026, 3, 9)]


def test_a_null_close_falls_back_to_the_bar_before_it(require_database):
    bars = [
        _bar("SPY", "2026-03-02", "15:59:00", 501.0),
        _bar("SPY", "2026-03-02", "16:00:00", None),
    ]

    closes = _closes(bars, {"SPY": (date(2026, 3, 2), date(2026, 3, 2))})

    assert closes == {"SPY": [(date(2026, 3, 2), 501.0)]}


def test_reads_only_inside_each_tickers_own_window(require_database):
    bars = [
        _bar("SPY", "2026-03-02", "16:00:00", 500.0),
        _bar("SPY", "2026-03-03", "16:00:00", 510.0),
        _bar("SPY", "2026-03-04", "16:00:00", 520.0),
        _bar("AAPL", "2026-03-02", "16:00:00", 190.0),
        _bar("AAPL", "2026-03-04", "16:00:00", 195.0),
    ]

    closes = _closes(
        bars,
        {"SPY": (date(2026, 3, 3), date(2026, 3, 3)), "AAPL": (date(2026, 3, 2), date(2026, 3, 4))},
    )

    assert closes == {
        "SPY": [(date(2026, 3, 3), 510.0)],
        "AAPL": [(date(2026, 3, 2), 190.0), (date(2026, 3, 4), 195.0)],
    }


def test_a_ticker_with_no_bars_is_absent(require_database):
    closes = _closes([], {"SPY": (date(2026, 3, 2), date(2026, 3, 3))})

    assert closes == {}
