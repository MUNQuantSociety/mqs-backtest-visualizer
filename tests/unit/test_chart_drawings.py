"""Chart drawings: the schema the Build page's Draw menu saves, and its routes."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.dependencies.current_user import require_current_user
from src.api.routes import chart_drawings as chart_drawings_api
from src.schemas.chart_drawings import MAX_DRAWINGS, ChartDrawing, ChartDrawingsUpdate
from src.services import chart_drawings as chart_drawings_service

OWNER = uuid.UUID(int=1)
TREND = {
    "id": "d1",
    "tool": "trend",
    "points": [{"date": "2026-04-01", "price": 250.5}, {"date": "2026-11-02", "price": 350}],
}
NOTE = {"id": "d2", "tool": "text", "points": [{"date": "2026-06-01", "price": 300}], "text": "Breakout"}


class TestSchema:
    def test_accepts_each_tool_with_its_points(self):
        assert ChartDrawing.model_validate(TREND).points[1].price == 350
        assert ChartDrawing.model_validate(NOTE).text == "Breakout"
        horizontal = {"id": "d3", "tool": "horizontal", "points": [{"date": "2026-06-01", "price": 1}]}
        assert ChartDrawing.model_validate(horizontal).tool == "horizontal"

    @pytest.mark.parametrize(
        "change",
        [
            {"points": TREND["points"][:1]},  # a trend line needs two points
            {"tool": "horizontal"},  # and a horizontal line one
            {"tool": "pentagram"},
            {"points": [{"date": "2026-13-01", "price": 1}, TREND["points"][1]]},
            {"points": [{"date": "1 April 2026", "price": 1}, TREND["points"][1]]},
            {"points": [{"date": "2026-04-01", "price": float("nan")}, TREND["points"][1]]},
            {"text": "only notes have text"},
            {"id": ""},
        ],
    )
    def test_rejects_a_malformed_drawing(self, change):
        with pytest.raises(ValidationError):
            ChartDrawing.model_validate({**TREND, **change})

    def test_a_note_needs_text_and_a_short_one(self):
        with pytest.raises(ValidationError):
            ChartDrawing.model_validate({**NOTE, "text": "   "})
        with pytest.raises(ValidationError):
            ChartDrawing.model_validate({**NOTE, "text": "x" * 201})

    def test_a_list_has_unique_ids_and_a_limit(self):
        with pytest.raises(ValidationError):
            ChartDrawingsUpdate.model_validate({"drawings": [TREND, TREND]})
        too_many = [{**TREND, "id": f"d{index}"} for index in range(MAX_DRAWINGS + 1)]
        with pytest.raises(ValidationError):
            ChartDrawingsUpdate.model_validate({"drawings": too_many})


@pytest.fixture
def saved(monkeypatch):
    """An in-memory ``app.chart_drawings``, keyed by owner and ticker."""
    rows: dict[tuple[uuid.UUID, str], SimpleNamespace] = {}

    @asynccontextmanager
    async def scope():
        yield object()

    async def get_drawings(session, *, owner_id, ticker):
        return rows.get((owner_id, ticker))

    async def replace_drawings(session, *, owner_id, ticker, drawings):
        if not drawings:
            rows.pop((owner_id, ticker), None)
            return None
        row = SimpleNamespace(drawings=drawings, updated_at=datetime(2026, 11, 17, tzinfo=timezone.utc))
        rows[(owner_id, ticker)] = row
        return row

    async def no_schema():
        return None

    monkeypatch.setattr(chart_drawings_service, "session_scope", scope)
    monkeypatch.setattr(chart_drawings_service, "ensure_schema", no_schema)
    monkeypatch.setattr(chart_drawings_service.drawings_repo, "get_drawings", get_drawings)
    monkeypatch.setattr(chart_drawings_service.drawings_repo, "replace_drawings", replace_drawings)
    return rows


@pytest.fixture
def client(saved):
    app = FastAPI()
    app.include_router(chart_drawings_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: OWNER
    return TestClient(app)


def test_a_ticker_with_nothing_saved_is_an_empty_list(client):
    response = client.get("/api/chart-drawings/AAPL")
    assert response.status_code == 200
    assert response.json() == {"ticker": "AAPL", "drawings": [], "updatedAt": None}


def test_saves_and_reads_back_the_whole_list(client, saved):
    response = client.put("/api/chart-drawings/aapl", json={"drawings": [TREND, NOTE]})
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["updatedAt"].startswith("2026-11-17")
    # Stored in the chart's own shape: no null text on drawings that are not notes.
    assert saved[(OWNER, "AAPL")].drawings[0] == TREND
    assert client.get("/api/chart-drawings/AAPL").json()["drawings"] == [
        {**TREND, "text": None},
        NOTE,
    ]


def test_an_empty_list_clears_the_ticker(client, saved):
    client.put("/api/chart-drawings/AAPL", json={"drawings": [TREND]})
    assert client.put("/api/chart-drawings/AAPL", json={"drawings": []}).status_code == 200
    assert (OWNER, "AAPL") not in saved


def test_drawings_are_kept_per_member(client, saved):
    client.put("/api/chart-drawings/AAPL", json={"drawings": [TREND]})
    client.app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=2)
    assert client.get("/api/chart-drawings/AAPL").json()["drawings"] == []


def test_rejects_a_bad_ticker_or_drawing(client):
    assert client.get("/api/chart-drawings/not a ticker").status_code == 422
    bad = {**TREND, "points": TREND["points"][:1]}
    assert client.put("/api/chart-drawings/AAPL", json={"drawings": [bad]}).status_code == 422
