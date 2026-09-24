"""The owner's runs still in flight, so any browser can show them before they finish.

History lists saved reports only; a queued or running job lives in the job
manager's memory until it succeeds. ``GET /backtests/active`` is how a browser
that did not submit a run learns it exists.
"""

import uuid
from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies.current_user import require_current_user
from src.api.routes.backtests import router
from src.workers import job_manager as jobs
from src.workers.report_job import RunSpec

OWNER = uuid.uuid4()


def spec(owner: uuid.UUID = OWNER, purpose: str = "user", minutes_ago: int = 0) -> RunSpec:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return RunSpec(uuid.uuid4(), owner, "Test run", "sample", "Sample", "builtin",
                   "sample:Strategy", None, date(2025, 1, 1), date(2025, 1, 3), 100_000,
                   "SPY", "test", {"universe": ["SPY"]}, created, purpose)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> jobs.JobManager:
    manager = jobs.JobManager(1)
    manager._pool = SimpleNamespace(submit=Mock(side_effect=lambda *args: Future()))
    manager._ipc = SimpleNamespace(dict=dict)
    monkeypatch.setattr(jobs, "create_sync_engine", Mock())
    monkeypatch.setattr(jobs.reports, "save", Mock())
    return manager


def register(manager: jobs.JobManager, request: RunSpec) -> RunSpec:
    manager.register(request)
    manager.submit(request.id)
    return request


def test_lists_a_queued_run_with_its_status(manager: jobs.JobManager) -> None:
    request = register(manager, spec())

    live = manager.live_summaries(OWNER)

    assert [(run.id, run.status) for run in live] == [(str(request.id), "queued")]


def test_lists_a_running_run_as_running(manager: jobs.JobManager) -> None:
    request = register(manager, spec())
    manager._lookup(request.id).state.update(status="running", progress_pct=40)

    assert [run.status for run in manager.live_summaries(OWNER)] == ["running"]


def test_lists_newest_first(manager: jobs.JobManager) -> None:
    older = register(manager, spec(minutes_ago=10))
    newer = register(manager, spec(minutes_ago=1))

    assert [run.id for run in manager.live_summaries(OWNER)] == [str(newer.id), str(older.id)]


def test_leaves_out_another_owner_s_runs(manager: jobs.JobManager) -> None:
    register(manager, spec(owner=uuid.uuid4()))

    assert manager.live_summaries(OWNER) == []


def test_leaves_out_a_run_that_has_finished(manager: jobs.JobManager) -> None:
    request = register(manager, spec())
    manager._lookup(request.id).future.set_result({"error": "Invalid strategy"})

    assert manager.live_summaries(OWNER) == []


def test_leaves_out_a_validation_run(manager: jobs.JobManager) -> None:
    register(manager, spec(purpose="validation"))

    assert manager.live_summaries(OWNER) == []


def test_is_empty_when_the_owner_has_no_runs(manager: jobs.JobManager) -> None:
    assert manager.live_summaries(OWNER) == []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, manager: jobs.JobManager) -> TestClient:
    monkeypatch.setattr(jobs, "_manager", manager)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: OWNER
    with TestClient(app) as test_client:
        yield test_client


def test_active_endpoint_returns_the_owner_s_live_runs_in_camel_case(
    client: TestClient, manager: jobs.JobManager,
) -> None:
    request = register(manager, spec())

    response = client.get("/api/backtests/active")

    assert response.status_code == 200
    body = response.json()
    assert [run["id"] for run in body] == [str(request.id)]
    assert body[0]["status"] == "queued"
    assert body[0]["strategyId"] == "sample"


def test_active_endpoint_is_empty_when_no_job_manager_is_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs, "_manager", None)

    response = client.get("/api/backtests/active")

    assert response.status_code == 200
    assert response.json() == []
