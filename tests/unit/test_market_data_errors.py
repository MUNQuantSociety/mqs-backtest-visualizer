"""A database that cannot be reached must not be reported as an empty window.

``fetch_historical_data`` used to answer both with the same empty DataFrame,
and the runner's guard turned that into ``NoMarketData: ... Check the ticker
coverage of public.market_data for this window`` — advice that sends a student
to fix dates that were never the problem. These tests pin the distinction.

No database and no network: the adapter is a stub returning the two envelopes
the real one produces.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from engine.contracts.errors import MarketDataUnavailable
from engine.core.utils import fetch_historical_data


class _StubPortfolio:
    """The two attributes ``fetch_historical_data`` actually reads."""

    def __init__(self, db: object, tickers: list[str]) -> None:
        self.db = db
        self.tickers = tickers
        self.logger = logging.getLogger("stub_portfolio")


class _FailingDB:
    """``EngineDBAdapter``'s error envelope: status='error', no rows."""

    def execute_query(self, sql, params=None, fetch=False):
        return {
            "status": "error",
            "message": 'could not connect to server: Connection timed out',
            "data": [],
        }


class _RaisingDB:
    """A driver that blows up rather than returning an envelope at all."""

    def execute_query(self, sql, params=None, fetch=False):
        raise OSError("network is unreachable")


class _EmptyDB:
    """A perfectly healthy query over a window that genuinely holds no bars."""

    def execute_query(self, sql, params=None, fetch=False):
        return {"status": "success", "data": []}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the parquet cache at an empty directory so the DB is consulted."""
    monkeypatch.setenv("MARKET_CACHE_DIR", str(tmp_path / "cache"))


@pytest.mark.parametrize("db", [_FailingDB(), _RaisingDB()])
def test_query_failure_raises_instead_of_looking_like_an_empty_window(db) -> None:
    portfolio = _StubPortfolio(db=db, tickers=["AAPL"])

    with pytest.raises(MarketDataUnavailable) as caught:
        fetch_historical_data(portfolio, "2025-01-02", "2025-01-31")

    message = str(caught.value)
    # The message has to point at the database, not at the student's dates.
    assert "did not complete" in message
    assert "not a gap in the data" in message


def test_a_genuinely_empty_window_still_returns_an_empty_frame() -> None:
    """The other half of the distinction: no rows is not an error."""
    portfolio = _StubPortfolio(db=_EmptyDB(), tickers=["AAPL"])

    frame = fetch_historical_data(portfolio, "2025-01-02", "2025-01-31")

    # Empty, not raised: the runner's NoMarketData guard is what names the
    # tickers and the window in that case, and it stays reachable.
    assert frame.empty
