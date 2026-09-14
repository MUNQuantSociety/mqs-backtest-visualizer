"""Reading a saved strategy's source back, and removing one.

Both exist for the same reason: a student iterating on an upload. Until the
source could be read back the editor always opened on the starter template, and
without a delete their failed attempts stayed in the drafts list forever.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.api.dependencies.current_user import require_current_user
from src.api.routes.strategies import router
from src.repositories.strategies import StrategyRow
from src.schemas.strategies import Strategy
from src.services import strategies as strategies_service


KEY = "user-momentum-abc12345"
OWNER = uuid.UUID(int=1)
SOMEONE_ELSE = uuid.UUID(int=2)


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_current_user] = lambda: OWNER
    return TestClient(app)


def _row(storage_key: str | None, owner_id: uuid.UUID | None = OWNER) -> StrategyRow:
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
            owner_id=owner_id,
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

    def test_404_for_another_members_strategy(self, api, store, monkeypatch):
        # Their source is theirs to edit. Indistinguishable from an unknown key
        # on purpose, so the endpoint confirms nothing about other uploads.
        _with_row(monkeypatch, _row("strategies/user-momentum-abc12345/", owner_id=SOMEONE_ELSE))

        response = api.get(f"/strategies/{KEY}/source")

        assert response.status_code == 404
        assert response.json() == {"detail": f"No stored source for strategy {KEY!r}."}
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
        # The caller's identity travels with the key: only their own uploads
        # are theirs to remove.
        remove.assert_awaited_once_with(KEY, owner_id=OWNER)

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
        remove.assert_awaited_once_with(key, owner_id=OWNER)

    def test_409_when_runs_still_reference_the_strategy(self, api, monkeypatch):
        monkeypatch.setattr(
            strategies_service,
            "delete_strategy",
            AsyncMock(side_effect=strategies_service.StrategyInUse(KEY)),
        )

        response = api.delete(f"/strategies/{KEY}")

        assert response.status_code == 409
        assert KEY in response.json()["detail"]

    def test_service_translates_the_commit_failure(self, monkeypatch):
        # The RESTRICT foreign key fires at commit, i.e. when the session scope
        # exits — not inside the repository call.
        @asynccontextmanager
        async def failing_scope():
            yield object()
            raise IntegrityError("DELETE", {}, Exception("violates foreign key"))

        monkeypatch.setattr(strategies_service, "ensure_schema", AsyncMock())
        monkeypatch.setattr(strategies_service, "session_scope", failing_scope)
        monkeypatch.setattr(
            strategies_service.strategies_repo, "delete_strategy", AsyncMock(return_value=True)
        )
        discard = Mock()
        monkeypatch.setattr(
            strategies_service.strategy_validation, "discard_stored_source", discard
        )

        with pytest.raises(strategies_service.StrategyInUse):
            asyncio.run(strategies_service.delete_strategy(KEY, owner_id=OWNER))
        discard.assert_not_called()

    def test_the_service_scopes_the_repository_delete_to_the_owner(self, monkeypatch):
        repo_delete = AsyncMock(return_value=False)
        monkeypatch.setattr(strategies_service, "ensure_schema", AsyncMock())
        monkeypatch.setattr(strategies_service, "session_scope", _NullSession)
        monkeypatch.setattr(strategies_service.strategies_repo, "delete_strategy", repo_delete)
        discard = Mock()
        monkeypatch.setattr(strategies_service.strategy_validation, "discard_stored_source", discard)

        removed = asyncio.run(strategies_service.delete_strategy(KEY, owner_id=OWNER))

        assert removed is False
        repo_delete.assert_awaited_once()
        assert repo_delete.await_args.kwargs == {"owner_id": OWNER}
        # Nothing was removed, so nothing in the store is touched either.
        discard.assert_not_called()


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
