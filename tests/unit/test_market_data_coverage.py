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
from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from server import app
from src.repositories import market_data as market_data_repo
from src.schemas.market_data import CoverageResponse, TickerCoverage
from src.services import backtests as backtests_service
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
        monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
        async def fake_coverage(_session, tickers):
            return {ticker: spans.get(ticker) for ticker in tickers}

        class _NullSession:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(market_data_repo, "ticker_coverage", fake_coverage)
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


@pytest.fixture(autouse=True)
def no_backfill(monkeypatch: pytest.MonkeyPatch):
    """These tests are about the intersection; the backfill has its own below."""
    monkeypatch.setattr(
        market_data_service, "settings",
        replace(market_data_service.settings, market_data_backfill_enabled=False),
    )


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


# ---------------------------------------------------------------------------
# The same coverage, enforced at submission
# ---------------------------------------------------------------------------
#
# The endpoint bounds the run form's picker. This is the other half: a window
# typed past those bounds is refused with a reason, instead of being queued and
# failing deep in the engine with an error about empty data.


@pytest.fixture
def stub_coverage(monkeypatch: pytest.MonkeyPatch):
    """Answer `coverage_for` directly, since the arithmetic is tested above."""
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")

    def install(response: CoverageResponse) -> None:
        async def fake(_tickers):
            return response

        monkeypatch.setattr(market_data_service, "coverage_for", fake)

    return install


COVERED = CoverageResponse(tickers=[], start="2020-01-02", end="2026-07-15", missing=[])


def test_a_window_inside_coverage_is_accepted(stub_coverage) -> None:
    stub_coverage(COVERED)
    asyncio.run(
        backtests_service._validated_coverage(
            ["AAPL"], date(2025, 1, 2), date(2026, 1, 2)
        )
    )


def test_a_window_running_past_the_last_bar_is_refused(stub_coverage) -> None:
    stub_coverage(COVERED)
    with pytest.raises(backtests_service.RunSubmissionError) as excinfo:
        asyncio.run(
            backtests_service._validated_coverage(
                ["AAPL"], date(2025, 1, 2), date(2026, 9, 1)
            )
        )
    # The message carries the range, so the fix is obvious without a second try.
    assert "2020-01-02 to 2026-07-15" in str(excinfo.value)


def test_a_window_starting_before_the_first_bar_is_refused(stub_coverage) -> None:
    stub_coverage(COVERED)
    with pytest.raises(backtests_service.RunSubmissionError):
        asyncio.run(
            backtests_service._validated_coverage(
                ["AAPL"], date(2019, 1, 2), date(2026, 1, 2)
            )
        )


def test_a_universe_with_no_data_is_refused_by_name(stub_coverage) -> None:
    stub_coverage(
        CoverageResponse(tickers=[], start=None, end=None, missing=["NOPE"])
    )
    with pytest.raises(backtests_service.RunSubmissionError) as excinfo:
        asyncio.run(
            backtests_service._validated_coverage(
                ["AAPL", "NOPE"], date(2025, 1, 2), date(2026, 1, 2)
            )
        )
    assert "NOPE" in str(excinfo.value)


def test_an_empty_universe_is_skipped_not_guessed_at(stub_coverage) -> None:
    """Nothing to check against, and refusing every such run would be wrong."""
    stub_coverage(COVERED)
    asyncio.run(
        backtests_service._validated_coverage([], date(2025, 1, 2), date(2026, 1, 2))
    )


# ---------------------------------------------------------------------------
# Automatic backfill of a ticker the table has never seen (database mode)
# ---------------------------------------------------------------------------


def _bars(ticker: str, days: list[date]):
    import pandas as pd

    return pd.DataFrame([
        {"ticker": ticker, "timestamp": pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(hours=16),
         "open_price": 10.0, "high_price": 11.0, "low_price": 9.0, "close_price": 10.5, "volume": 1000}
        for day in days
    ])


@pytest.fixture
def backfill_on(monkeypatch: pytest.MonkeyPatch, stub_repo):
    """Database mode with the backfill switched on, the provider and the insert stubbed."""
    monkeypatch.setattr(
        market_data_service, "settings",
        replace(market_data_service.settings, market_data_backfill_enabled=True),
    )
    # The exchange lookup constructs the provider client, which refuses
    # without a key; the transport itself is stubbed below.
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    state = {"spans": {}, "fetched": [], "inserted": []}

    def install(spans):
        state["spans"] = dict(spans)
        stub_repo(state["spans"])

        async def fake_coverage(_session, tickers):
            return {ticker: state["spans"].get(ticker) for ticker in tickers}

        def fake_fetch(tickers, start, end, *, require_all=True):
            (ticker,) = tickers
            state["fetched"].append((ticker, start, end))
            days = [start + timedelta(days=i) for i in range(3)]
            return _bars(ticker, days)

        async def fake_insert(_session, rows):
            state["inserted"].extend(rows)
            # The table now has the ticker: the re-read must see it.
            if rows:
                state["spans"][rows[0]["ticker"]] = (rows[0]["date"], rows[-1]["date"])
            return len(rows)

        monkeypatch.setattr(market_data_repo, "ticker_coverage", fake_coverage)
        monkeypatch.setattr(market_data_service, "fetch_daily_history", fake_fetch)
        monkeypatch.setattr(market_data_repo, "insert_daily_bars", fake_insert)
        # The provider places NEWCO on the NYSE; anything else it does not know.
        monkeypatch.setattr(
            market_data_service.FMPMarketData, "search_symbols",
            lambda self, query, limit=100: (
                [{"symbol": "NEWCO", "exchange": "NYSE"}] if query == "NEWCO" else []
            ),
        )
        return state

    return install


def test_a_missing_ticker_is_backfilled_over_the_universes_window_and_coverage_rereads(backfill_on) -> None:
    state = backfill_on({"AAPL": (date(2024, 1, 2), date(2026, 7, 15)), "NEWCO": None})

    result = asyncio.run(market_data_service.coverage_for(["AAPL", "NEWCO"]))

    assert state["fetched"] == [("NEWCO", date(2024, 1, 2), date(2026, 7, 15))]
    assert result.missing == []
    reported = {item.ticker: item for item in result.tickers}
    assert reported["NEWCO"].first_bar == "2024-01-02"
    row = state["inserted"][0]
    # The venue is the provider's, not invented: the history endpoint does not
    # say where a bar traded, and the table's column is NOT NULL.
    assert row["ticker"] == "NEWCO" and row["exchange"] == "NYSE"
    assert row["timestamp"].hour == 16 and row["date"] == date(2024, 1, 2)
    assert set(row) == {"ticker", "timestamp", "date", "exchange", "open_price", "high_price",
                        "low_price", "close_price", "volume"}


def test_a_symbol_the_provider_does_not_place_falls_back_to_the_seeds_exchange(backfill_on) -> None:
    state = backfill_on({"AAPL": (date(2024, 1, 2), date(2026, 7, 15)), "OTHER": None})

    asyncio.run(market_data_service.coverage_for(["AAPL", "OTHER"]))

    assert state["inserted"][0]["exchange"] == "NASDAQ"


def test_the_backfill_is_off_unless_a_deployment_turns_it_on() -> None:
    # Settings are read once at import, so this checks the default the test
    # process imported with — MARKET_DATA_BACKFILL_ENABLED unset — and that
    # the default is not derived from APP_ENV, which itself defaults to
    # "development": an unconfigured process must never write the live table.
    import os

    from src.core.config import settings

    assert "MARKET_DATA_BACKFILL_ENABLED" not in os.environ
    assert settings.app_env == "development"
    assert settings.market_data_backfill_enabled is False


def test_with_nothing_to_anchor_to_the_backfill_takes_the_last_two_years(backfill_on) -> None:
    state = backfill_on({"NEWCO": None})

    asyncio.run(market_data_service.coverage_for(["NEWCO"]))

    ((_, start, end),) = state["fetched"]
    assert (end - start).days == 730 and end < date.today()


def test_a_ticker_the_provider_has_nothing_for_stays_missing_without_a_second_read(backfill_on, monkeypatch) -> None:
    state = backfill_on({"AAPL": (date(2024, 1, 2), date(2026, 7, 15)), "NOPE": None})
    monkeypatch.setattr(
        market_data_service, "fetch_daily_history",
        lambda tickers, start, end, *, require_all=True: _bars("NOPE", []),
    )

    result = asyncio.run(market_data_service.coverage_for(["AAPL", "NOPE"]))

    assert result.missing == ["NOPE"] and state["inserted"] == []


def test_a_provider_outage_during_backfill_is_not_a_coverage_error(backfill_on, monkeypatch) -> None:
    from src.services.market_data import FMPUnavailable

    def down(tickers, start, end, *, require_all=True):
        raise FMPUnavailable("FMP is down")

    backfill_on({"AAPL": (date(2024, 1, 2), date(2026, 7, 15)), "NEWCO": None})
    monkeypatch.setattr(market_data_service, "fetch_daily_history", down)

    result = asyncio.run(market_data_service.coverage_for(["AAPL", "NEWCO"]))

    assert result.missing == ["NEWCO"]


def test_the_backfill_never_runs_when_switched_off(backfill_on, monkeypatch) -> None:
    state = backfill_on({"NEWCO": None})
    monkeypatch.setattr(
        market_data_service, "settings",
        replace(market_data_service.settings, market_data_backfill_enabled=False),
    )

    result = asyncio.run(market_data_service.coverage_for(["NEWCO"]))

    assert state["fetched"] == [] and result.missing == ["NEWCO"]


def test_the_backfill_never_runs_in_fmp_mode(backfill_on, monkeypatch) -> None:
    state = backfill_on({"NEWCO": None})
    monkeypatch.setenv("MARKET_DATA_SOURCE", "fmp")
    monkeypatch.setattr(market_data_service, "_fmp_coverage", _async_return({"NEWCO": None}))

    asyncio.run(market_data_service.coverage_for(["NEWCO"]))

    assert state["fetched"] == []


def _async_return(value):
    async def call(_tickers):
        return value

    return call
