"""Simulations are disclosed and have a separate authenticated history API."""

import asyncio
import copy
import csv
import io
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import current_user
from src.api.routes.backtests import router
from src.models import BacktestReport
from src.repositories import reports
from src.schemas.backtests import BacktestListResponse, BacktestStatus
from src.services import backtests, starter_reports
from src.services.report_exports import export_report


OWNER = uuid.UUID("00000000-0000-0000-0000-000000000201")


def _detail():
    return starter_reports._build_report(
        owner_id=OWNER, strategy_key="portfolio_1",
        strategy_name="Volatility Momentum", universe=["AAPL"],
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("metadata", [
    {"purpose": "example"},
    {"purpose": "user", "starterExample": {"generated": True}},
    {"purpose": "user", "marketData": {"source": "bundled_starter_example"}},
])
def test_legacy_examples_are_disclosed_without_rewriting_stored_document(metadata):
    detail = _detail()
    payload = reports.document(detail)
    payload["reportMetadata"] = metadata
    original = copy.deepcopy(payload)
    row = BacktestReport(
        id=uuid.UUID(detail.id), owner_id=OWNER, created_at=datetime.now(timezone.utc),
        name="portfolio_1", strategy_key="portfolio_1", version=1, results=payload,
    )

    result = reports.to_detail(row)

    assert result.name == "Example: Volatility Momentum (simulated)"
    assert result.report_metadata["purpose"] == "example"
    assert result.report_metadata["starterExample"]["message"] == reports.EXAMPLE_WARNING
    assert row.results == original
    assert row.name == "portfolio_1"


def test_real_report_label_and_metadata_are_preserved():
    detail = _detail()
    payload = reports.document(detail)
    payload["reportMetadata"] = {"purpose": "user", "marketData": {"source": "fmp"}}
    row = BacktestReport(
        id=uuid.UUID(detail.id), owner_id=OWNER, created_at=datetime.now(timezone.utc),
        name="My actual run", strategy_key="portfolio_1", version=1, results=payload,
    )

    result = reports.to_detail(row)

    assert result.name == "My actual run"
    assert result.report_metadata == payload["reportMetadata"]


def test_staged_examples_require_an_owner_before_touching_session():
    session = SimpleNamespace(add_all=Mock(), flush=AsyncMock())
    with pytest.raises(ValueError, match="requires an owner"):
        asyncio.run(reports.add_completed_reports(session, None, [_detail()]))
    session.add_all.assert_not_called()
    session.flush.assert_not_awaited()


def test_examples_route_precedes_detail_and_forwards_owner_and_filters(monkeypatch):
    listing = AsyncMock(return_value=BacktestListResponse(items=[], total=0, page=2, page_size=10))
    detail = AsyncMock(side_effect=AssertionError("examples is not a report ID"))
    monkeypatch.setattr(backtests, "list_example_backtests", listing)
    monkeypatch.setattr(backtests, "get_backtest", detail)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[current_user.require_current_user] = lambda: OWNER

    with TestClient(app) as client:
        response = client.get("/api/backtests/examples", params={
            "search": "Momentum", "strategyId": "portfolio_1", "status": "completed",
            "page": 2, "pageSize": 10,
        })

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 2, "pageSize": 10}
    listing.assert_awaited_once_with(
        owner_id=OWNER, search="Momentum", status=BacktestStatus.COMPLETED,
        strategy_id="portfolio_1", page=2, page_size=10,
    )
    detail.assert_not_awaited()


def test_examples_require_authentication_before_listing(monkeypatch):
    monkeypatch.setattr(current_user, "settings", replace(
        current_user.settings, app_env="production", auth_allow_dev_identity=False,
    ))
    listing = AsyncMock(side_effect=AssertionError("anonymous callers cannot list examples"))
    monkeypatch.setattr(backtests, "list_example_backtests", listing)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    with TestClient(app) as client:
        response = client.get("/api/backtests/examples")

    assert response.status_code == 401
    listing.assert_not_awaited()


def test_non_completed_example_filter_does_not_query_database(monkeypatch):
    schema = AsyncMock(side_effect=AssertionError("no unfinished example reports exist"))
    monkeypatch.setattr(backtests, "ensure_schema", schema)

    result = asyncio.run(backtests.list_example_backtests(
        owner_id=OWNER, status=BacktestStatus.RUNNING, page=3, page_size=5,
    ))

    assert result.items == []
    assert result.total == 0
    assert result.page == 3 and result.page_size == 5
    schema.assert_not_awaited()


@pytest.mark.parametrize("filename", ["equity.csv", "trades.csv", "metrics.csv"])
def test_example_csv_exports_keep_simulation_disclosure_in_every_row(filename):
    detail = _detail()

    reader = csv.DictReader(io.StringIO(export_report(detail, filename).content))
    rows = list(reader)

    assert reader.fieldnames[:2] == ["reportType", "reportName"]
    assert rows
    assert all(row["reportType"] == "simulated_example" for row in rows)
    assert all(row["reportName"] == detail.name for row in rows)
    assert "(simulated)" in rows[0]["reportName"]


def test_empty_example_trade_csv_keeps_disclosure_columns():
    detail = _detail()
    detail.trades = []

    reader = csv.DictReader(io.StringIO(export_report(detail, "trades.csv").content))

    assert reader.fieldnames[:2] == ["reportType", "reportName"]
    assert list(reader) == []


@pytest.mark.parametrize("filename,expected_fields", [
    ("equity.csv", ["date", "equity", "benchmark"]),
    ("trades.csv", ["id", "symbol", "side", "entryDate", "exitDate", "entryPrice",
                    "exitPrice", "quantity", "pnl", "returnPct", "fees"]),
    ("metrics.csv", ["metric", "value", "unavailableReason"]),
])
def test_real_csv_columns_remain_unchanged(filename, expected_fields):
    detail = _detail()
    detail.report_metadata = {"purpose": "user"}
    detail.name = "My actual run"

    reader = csv.DictReader(io.StringIO(export_report(detail, filename).content))

    assert reader.fieldnames == expected_fields
    assert all(set(row) == set(expected_fields) for row in reader)
