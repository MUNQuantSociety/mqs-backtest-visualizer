"""Storage outages return an actionable error, never backend bucket details."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from server import app
from src.integrations.strategy_store import StrategyStoreError
from src.services import strategies


def test_strategy_upload_storage_outage_is_sanitized(monkeypatch):
    from src.api.dependencies.current_user import require_current_user
    monkeypatch.setitem(app.dependency_overrides, require_current_user, lambda: "test-owner")
    monkeypatch.setattr(
        strategies,
        "submit_strategy",
        AsyncMock(side_effect=StrategyStoreError("AccessDenied: private-bucket-name")),
    )
    response = TestClient(app).post(
        "/api/strategies",
        json={
            "name": "sample",
            "source": "class MyStrategy: pass\n",
        },
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert "try again" in response.json()["detail"]
    assert "private-bucket" not in response.text
