"""Reading a saved strategy's source back, and removing one.

Both exist for the same reason: a student iterating on an upload. Until the
source could be read back the editor always opened on the starter template, and
without a delete their failed attempts stayed in the drafts list forever.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.strategies import router
from src.repositories.strategies import StrategyRow
from src.schemas.strategies import Strategy
from src.services import strategies as strategies_service


KEY = "user-momentum-abc12345"


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _row(storage_key: str | None) -> StrategyRow:
    return StrategyRow(
        strategy=SimpleNamespace(
            key=KEY,
            name="Momentum",
            class_path=None,
            description="",
            status="active",
            tags=["user"],
            param_specs=[],
            universe=["AAPL"],
            storage_key=storage_key,
            validation_run_id=None,
            # NULL is how every uploaded-file strategy reads.
            authoring=None,
        ),
        run_count=0,
        best_sharpe=None,
        best_return=None,
        last_run_at=None,
    )


@pytest.fixture
def store(monkeypatch) -> Mock:
    """Stands in for the object store the source is read out of."""
    store = Mock()
    monkeypatch.setattr(strategies_service, "get_strategy_store", lambda: store)
    monkeypatch.setattr(strategies_service, "ensure_schema", AsyncMock())
    return store


def _with_row(monkeypatch, row: StrategyRow | None) -> None:
    monkeypatch.setattr(
        strategies_service.strategies_repo,
        "get_strategy_row",
        AsyncMock(return_value=row),
    )
    # The service opens a session around the lookup; the repository is mocked,
    # so the session only has to be an async context manager.
    monkeypatch.setattr(strategies_service, "session_scope", _NullSession)


class _NullSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_):
        return False


class TestGetSource:
    def test_returns_the_stored_python(self, api, store, monkeypatch):
        _with_row(monkeypatch, _row("strategies/user-momentum-abc12345/"))
        store.get.return_value = "class Momentum(BasePortfolio):\n    pass\n"

        response = api.get(f"/strategies/{KEY}/source")

        assert response.status_code == 200
        # Same shape as GET /strategies/template, so one editor loads either.
        # An uploaded file reopens as the file: no fragment to hand back.
        assert response.json() == {
            "filename": "strategy.py",
            "source": "class Momentum(BasePortfolio):\n    pass\n",
            "body": None,
            "indicators": None,
            "state": None,
        }
        store.get.assert_called_once_with("strategies/user-momentum-abc12345/", "strategy.py")

    def test_404_for_an_unknown_key(self, api, store, monkeypatch):
        _with_row(monkeypatch, None)

        assert api.get(f"/strategies/{KEY}/source").status_code == 404
        store.get.assert_not_called()

    def test_404_for_a_built_in_with_no_stored_package(self, api, store, monkeypatch):
        # Engine built-ins were never uploaded, so there is no source to hand out.
        _with_row(monkeypatch, _row(None))

        assert api.get(f"/strategies/{KEY}/source").status_code == 404
        store.get.assert_not_called()

    def test_404_when_the_row_outlived_its_package(self, api, store, monkeypatch):
        _with_row(monkeypatch, _row("strategies/gone/"))
        store.get.side_effect = KeyError("strategy.py")

        # Reported as absent rather than a 500: the caller can do nothing about
        # it, and the registry row is still real.
        assert api.get(f"/strategies/{KEY}/source").status_code == 404

    def test_the_route_does_not_shadow_the_by_key_route(self, api, monkeypatch):
        monkeypatch.setattr(
            strategies_service,
            "get_strategy",
            AsyncMock(
                return_value=Strategy(
                    id=KEY, name="Momentum", class_name="Momentum", description="",
                    status="active",
                )
            ),
        )

        assert api.get(f"/strategies/{KEY}").status_code == 200


class TestDelete:
    def test_204_when_removed(self, api, monkeypatch):
        remove = AsyncMock(return_value=True)
        monkeypatch.setattr(strategies_service, "delete_strategy", remove)

        response = api.delete(f"/strategies/{KEY}")

        assert response.status_code == 204
        assert response.content == b""
        remove.assert_awaited_once_with(KEY)

    def test_404_when_there_was_nothing_to_remove(self, api, monkeypatch):
        monkeypatch.setattr(
            strategies_service, "delete_strategy", AsyncMock(return_value=False)
        )

        assert api.delete(f"/strategies/{KEY}").status_code == 404

    def test_a_uuid_shaped_key_is_still_just_a_key(self, api, monkeypatch):
        remove = AsyncMock(return_value=True)
        monkeypatch.setattr(strategies_service, "delete_strategy", remove)

        key = str(uuid.uuid4())
        assert api.delete(f"/strategies/{key}").status_code == 204
        remove.assert_awaited_once_with(key)


class TestFragmentAuthoredSource:
    """A draft reopens as the fragment it was written as, not as the file.

    Step 3 of the plan: the body and indicator spec live on the registry row,
    so the editor can put a member back in front of their own eight lines
    rather than the generated boilerplate around them.
    """

    def test_the_stored_fragment_comes_back(self, api, store, monkeypatch):
        row = _row("strategies/user-momentum-abc12345/")
        row.strategy.authoring = {
            "body": "for ticker in self.tickers:\n    pass",
            "indicators": [
                {"attribute": "fast", "indicator": "SimpleMovingAverage", "params": {"period": 20}}
            ],
            "state": {"last_price": {}},
        }
        _with_row(monkeypatch, row)
        store.get.return_value = "class MyStrategy(BasePortfolio):\n    pass\n"

        payload = api.get(f"/strategies/{KEY}/source").json()

        assert payload["body"] == "for ticker in self.tickers:\n    pass"
        assert payload["indicators"] == [
            {"attribute": "fast", "indicator": "SimpleMovingAverage", "params": {"period": 20}}
        ]
        assert payload["state"] == {"last_price": {}}
        # The assembled file is still there — it is what actually runs.
        assert "class MyStrategy" in payload["source"]
