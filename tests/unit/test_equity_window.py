"""Dashboard period contract without a database, workers, or new backtest jobs."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.api.routes.backtests import router
from src.schemas.backtests import BacktestDetail, EquityPoint
from src.services import backtests


@pytest.fixture
def detail():
    # Only the equity endpoint's inputs; no lifetime metrics belong in its response.
    return BacktestDetail.model_construct(
        id="run-1", strategy_id="strategy-1", symbol="SPY",
        equity_curve=[
            EquityPoint(date=day, equity=100 + i * 10, benchmark=100 + i * 5)
            for i, day in enumerate([
                "2018-02-28", "2020-02-28", "2022-02-28", "2023-02-27",
                "2023-02-28", "2024-02-29", "2024-03-01",
            ])
        ],
    )


@pytest.fixture
def client(monkeypatch, detail):
    monkeypatch.setattr(backtests, "get_backtest", AsyncMock(return_value=detail))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    from src.api.dependencies.current_user import require_current_user
    app.dependency_overrides[require_current_user] = lambda: 'test-owner'
    return TestClient(app)


@pytest.mark.parametrize(("period", "start", "count"), [
    ("1y", "2023-02-28", 2), ("2y", "2022-02-28", 4),
    ("5y", "2019-02-28", 5), ("max", None, 6),
])
def test_periods_are_filtered_on_backend(client, detail, period, start, count):
    response = client.get(f"/api/backtests/run-1/equity?period={period}&endDate=2024-02-29")
    assert response.status_code == 200
    body = response.json()
    assert len(body["equityCurve"]) == count
    assert body["equityCurve"][-1] == {"date": "2024-02-29", "equity": 150, "benchmark": 125}
    assert body["window"] == {
        "period": period, "requestedStart": start, "requestedEnd": "2024-02-29",
        "availableStart": "2018-02-28", "availableEnd": "2024-03-01",
    }
    assert "metrics" not in body and "trades" not in body
    assert len(detail.equity_curve) == 7  # No mutation of the saved report.


def test_common_anchor_does_not_pull_stale_runs_into_a_recent_window(client):
    response = client.get("/api/backtests/run-1/equity?period=1y&endDate=2026-09-09")
    assert response.status_code == 200
    assert response.json()["equityCurve"] == []
    assert response.json()["window"]["availableEnd"] == "2024-03-01"


@pytest.mark.parametrize("query", [
    "period=3y&endDate=2024-01-01", "period=1y&endDate=invalid",
    "period=5y&endDate=0001-01-01", "period=max", "endDate=2024-01-01",
])
def test_invalid_windows_are_rejected(client, query):
    assert client.get(f"/api/backtests/run-1/equity?{query}").status_code == 422


def test_missing_run_is_404(client, monkeypatch):
    monkeypatch.setattr(backtests, "get_backtest", AsyncMock(return_value=None))
    assert client.get("/api/backtests/missing/equity?period=max&endDate=2024-01-01").status_code == 404


def test_empty_run_returns_empty_available_bounds(client, detail):
    detail.equity_curve = []
    body = client.get("/api/backtests/run-1/equity?period=5y&endDate=2024-01-01").json()
    assert body["equityCurve"] == []
    assert body["window"]["availableStart"] is None
    assert body["window"]["availableEnd"] is None
