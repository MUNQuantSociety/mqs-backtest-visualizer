"""Submission cleanup and thread boundaries; no database, S3 or app lifespan."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.schemas.strategies import StrategySubmission
from src.services import strategies as service
from src.services.strategy_validation.template import STARTER_SOURCE

KEY = "user-cleanup-regression"
STORAGE_KEY = f"strategies/{KEY}/"


@pytest.fixture
def isolated_service(monkeypatch):
    """Replace every external dependency, including the configured S3 store."""
    session = object()

    @asynccontextmanager
    async def session_scope():
        yield session

    get = AsyncMock(return_value=None)
    create = AsyncMock()
    store = Mock(return_value=STORAGE_KEY)
    discard = Mock()
    begin = AsyncMock(return_value=("Validation queued", None))
    monkeypatch.setattr(service, "session_scope", session_scope)
    monkeypatch.setattr(service, "ensure_schema", AsyncMock())
    monkeypatch.setattr(service, "_generate_key", lambda name: KEY)
    monkeypatch.setattr(service, "_begin_validation", begin)
    monkeypatch.setattr(service.strategies_repo, "get_strategy", get)
    monkeypatch.setattr(service.strategies_repo, "create_strategy", create)
    monkeypatch.setattr(service.strategy_validation, "migrate_staged_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(service.strategy_validation, "store_strategy_source", store)
    monkeypatch.setattr(service.strategy_validation, "discard_stored_source", discard)
    return SimpleNamespace(session=session, get=get, create=create, store=store, discard=discard, begin=begin)


def submission():
    return StrategySubmission(name="Cleanup regression", source=STARTER_SOURCE, filename="strategy.py")


def test_confirmed_absence_deletes_after_read_transaction_and_off_event_loop(isolated_service, monkeypatch):
    calls = []
    event_loop_thread = threading.get_ident()

    @asynccontextmanager
    async def read_transaction():
        yield isolated_service.session
        calls.append("read transaction finished")

    def discard(key):
        assert key == KEY
        assert threading.get_ident() != event_loop_thread
        calls.append("discard")

    monkeypatch.setattr(service, "session_scope", read_transaction)
    isolated_service.discard.side_effect = discard
    asyncio.run(service._discard_unregistered_source(KEY))
    isolated_service.get.assert_awaited_once_with(isolated_service.session, KEY)
    isolated_service.discard.assert_called_once_with(KEY)
    assert calls == ["read transaction finished", "discard"]


@pytest.mark.parametrize("status", ["active", "validating", "failed_validation", "archived"])
def test_any_registered_strategy_keeps_its_source(isolated_service, status):
    isolated_service.get.return_value = SimpleNamespace(status=status, storage_key=STORAGE_KEY)
    asyncio.run(service._discard_unregistered_source(KEY))
    isolated_service.get.assert_awaited_once_with(isolated_service.session, KEY)
    isolated_service.discard.assert_not_called()


@pytest.mark.parametrize("failure_phase", ["open", "read", "close"])
def test_uncertain_registry_read_retains_source(isolated_service, monkeypatch, caplog, failure_phase):
    failure = RuntimeError("fake registry unavailable")

    @asynccontextmanager
    async def uncertain_transaction():
        if failure_phase == "open":
            raise failure
        yield isolated_service.session
        if failure_phase == "close":
            raise failure

    monkeypatch.setattr(service, "session_scope", uncertain_transaction)
    if failure_phase == "read":
        isolated_service.get.side_effect = failure
    asyncio.run(service._discard_unregistered_source(KEY))
    isolated_service.discard.assert_not_called()
    assert f"Retaining source for {KEY}" in caplog.text


def test_cleanup_failure_does_not_escape_the_helper(isolated_service, caplog):
    isolated_service.discard.side_effect = RuntimeError("fake cleanup failure")
    asyncio.run(service._discard_unregistered_source(KEY))
    isolated_service.discard.assert_called_once_with(KEY)
    assert "fake cleanup failure" in caplog.text


@pytest.mark.parametrize("write_phase", ["insert", "commit"])
@pytest.mark.parametrize("cleanup_state", ["absent", "registered", "uncertain"])
def test_submission_preserves_original_registry_error_and_checks_ownership(
    isolated_service, monkeypatch, write_phase, cleanup_state,
):
    original = RuntimeError("fake registry write failure")
    scopes = 0

    @asynccontextmanager
    async def transaction():
        nonlocal scopes
        scopes += 1
        this_scope = scopes
        yield isolated_service.session
        if this_scope == 1 and write_phase == "commit":
            raise original

    monkeypatch.setattr(service, "session_scope", transaction)
    if write_phase == "insert":
        isolated_service.create.side_effect = original
    if cleanup_state == "registered":
        isolated_service.get.return_value = SimpleNamespace(storage_key=STORAGE_KEY)
    elif cleanup_state == "uncertain":
        isolated_service.get.side_effect = RuntimeError("fake recovery read failure")

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(service.submit_strategy(submission()))
    assert caught.value is original
    assert scopes == 2
    isolated_service.store.assert_called_once()
    isolated_service.get.assert_awaited_once_with(isolated_service.session, KEY)
    isolated_service.begin.assert_not_awaited()
    if cleanup_state == "absent":
        isolated_service.discard.assert_called_once_with(KEY)
    else:
        isolated_service.discard.assert_not_called()


def test_submission_stores_off_event_loop_before_registry_and_validation(isolated_service, monkeypatch):
    event_loop_thread = threading.get_ident()
    calls = []

    def store(key, source, config):
        assert threading.get_ident() != event_loop_thread
        assert key == KEY and source == STARTER_SOURCE
        assert config["PORTFOLIO_ID"] == KEY
        calls.append("store")
        return STORAGE_KEY

    async def create(session, **fields):
        assert threading.get_ident() == event_loop_thread
        assert session is isolated_service.session
        assert fields["storage_key"] == STORAGE_KEY
        calls.append("registry insert")

    @asynccontextmanager
    async def transaction():
        yield isolated_service.session
        calls.append("registry committed")

    async def begin(*args, **kwargs):
        calls.append("validation")
        return "Validation queued", None

    monkeypatch.setattr(service, "session_scope", transaction)
    isolated_service.store.side_effect = store
    isolated_service.create.side_effect = create
    isolated_service.begin.side_effect = begin
    result = asyncio.run(service.submit_strategy(submission()))
    assert result.id == KEY
    assert calls == ["store", "registry insert", "registry committed", "validation"]
    isolated_service.discard.assert_not_called()
