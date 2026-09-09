from __future__ import annotations

import csv
import io
import json
import math
from datetime import date
from statistics import fmean, stdev
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server import app
from src.schemas.backtests import BacktestDetail, PerformanceMetrics
from src.services.report_exports import export_report
from src.services.reporting import daily_metrics, open_positions
from src.services.trade_pairing import pair_fills


def test_daily_statistics_match_frontend_conventions():
    equity = [100, 102, 101, 104, 103]
    got = daily_metrics(equity, 99)
    returns = [b / a - 1 for a, b in zip(equity, equity[1:])]
    excess = [r - 0.02 / 252 for r in returns]
    downside = [x * x for x in excess if x < 0]
    assert got["total_return"] == pytest.approx(103 / 99 - 1)
    assert got["cagr"] == pytest.approx((103 / 100) ** (252 / 4) - 1)
    assert got["sharpe"] == pytest.approx(
        fmean(excess) / stdev(excess) * math.sqrt(252)
    )
    assert got["sortino"] == pytest.approx(
        fmean(excess) / math.sqrt(fmean(downside)) * math.sqrt(252)
    )
    assert got["volatility"] == pytest.approx(stdev(returns) * math.sqrt(252))
    assert got["max_drawdown"] == pytest.approx(101 / 102 - 1)


def test_short_flat_and_invalid_curves_are_explicit():
    assert daily_metrics([100], 100)["sharpe"] is None
    assert daily_metrics([100, 100, 100], 100)["sharpe"] is None
    assert daily_metrics([100, 100, 100], 100)["volatility"] == 0
    assert daily_metrics([100, -1], 100)["cagr"] is None
    for equity in ([], [float("nan")], [float("inf")]):
        with pytest.raises(ValueError):
            daily_metrics(equity, 100)


def test_market_dates_and_unrealized_marks_do_not_change_realized_pnl():
    trades = pair_fills(
        [
            {
                "timestamp": "2026-03-03T01:00:00Z",
                "ticker": "AAPL",
                "signal_type": "BUY",
                "shares": 3,
                "fill_price": 100,
            },
            {
                "timestamp": "2026-03-04T01:00:00Z",
                "ticker": "AAPL",
                "signal_type": "SELL",
                "shares": 1,
                "fill_price": 110,
            },
        ],
        market_timezone="America/New_York",
    )
    assert trades[0].entry_date == "2026-03-02"
    assert trades[0].exit_date == "2026-03-03"
    assert trades[0].pnl == 10
    assert trades[1].pnl == 0
    positions = open_positions(trades, {"AAPL": 120})
    assert positions[0]["unrealizedPnl"] == 40
    assert positions[0]["marketValue"] == 240
    assert open_positions(trades, {})[0]["unrealizedPnl"] is None


@pytest.fixture
def report():
    return BacktestDetail(
        id="c0096094-0d53-4b56-8f18-6138730dbe59",
        name="A real report",
        strategy_id="example",
        strategy_name="Example",
        symbol="AAPL",
        timeframe="1d",
        status="completed",
        start_date="2026-03-02",
        end_date="2026-03-04",
        created_at="2026-03-05T12:00:00Z",
        initial_capital=100,
        final_equity=102,
        total_return=0.02,
        sharpe=0,
        max_drawdown=-0.01,
        metrics=PerformanceMetrics(
            total_return=0.02,
            cagr=0,
            sharpe=0,
            sortino=0,
            max_drawdown=-0.01,
            volatility=0.1,
            win_rate=1,
            profit_factor=0,
            total_trades=1,
            unavailable={"profitFactor": "No losing trades."},
        ),
        equity_curve=[
            {"date": "2026-03-02", "equity": 100, "benchmark": 100},
            {"date": "2026-03-04", "equity": 102, "benchmark": None},
        ],
        trades=[],
        report_metadata={"reportVersion": 1},
    )


def test_exports_share_detail_contract_and_preserve_missing_values(report):
    assert json.loads(
        export_report(report, "report.json").content
    ) == report.model_dump(mode="json", by_alias=True)
    rows = list(
        csv.DictReader(io.StringIO(export_report(report, "equity.csv").content))
    )
    assert rows[-1] == {"date": "2026-03-04", "equity": "102.0", "benchmark": ""}
    metrics = list(
        csv.DictReader(io.StringIO(export_report(report, "metrics.csv").content))
    )
    pf = next(row for row in metrics if row["metric"] == "profitFactor")
    assert pf["value"] == ""
    assert pf["unavailableReason"] == "No losing trades."
    assert export_report(report, "trades.csv").content.startswith("id,symbol,side,")


def test_export_route_rejects_unknown_and_unfinished_runs(monkeypatch, report):
    lookup = AsyncMock(return_value=report)
    monkeypatch.setattr("src.services.backtests.get_backtest", lookup)
    client = TestClient(app)
    response = client.get(f"/api/backtests/{report.id}/exports/equity.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="equity.csv"' in response.headers["content-disposition"]
    assert client.get(f"/api/backtests/{report.id}/exports/.env").status_code == 404
    report.status = "running"
    assert (
        client.get(f"/api/backtests/{report.id}/exports/equity.csv").status_code == 409
    )
    lookup.return_value = None
    assert client.get("/api/backtests/missing/exports/equity.csv").status_code == 404
