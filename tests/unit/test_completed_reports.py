"""Successful completion is the only event allowed to persist a report."""

import asyncio
import time
import uuid
from concurrent.futures import Future
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.models import BacktestReport
from src.repositories import reports
from src.schemas.backtests import EquityPoint
from src.workers import job_manager as jobs
from src.workers.report_job import RunSpec


def spec(owner=None):
    return RunSpec(uuid.uuid4(), owner or uuid.uuid4(), "Test run", "sample", "Sample",
                   "builtin", "sample:Strategy", None, date(2025, 1, 1), date(2025, 1, 3),
                   100_000, "SPY", "test", {"universe": ["SPY"]}, datetime.now(timezone.utc))


def success(job):
    detail = job.spec.empty_detail("completed", 100)
    detail.equity_curve = [EquityPoint(date="2025-01-03", equity=100_001)]
    detail.final_equity = 100_001
    return {"report": detail.model_dump(mode="json", by_alias=True)}


@pytest.fixture
def manager(monkeypatch):
    manager = jobs.JobManager(1)
    manager._pool = SimpleNamespace(submit=Mock(side_effect=lambda *args: Future()))
    manager._ipc = SimpleNamespace(dict=dict)
    engine = SimpleNamespace(dispose=Mock())
    connect = Mock(return_value=engine)
    save = Mock()
    monkeypatch.setattr(jobs, "create_sync_engine", connect)
    monkeypatch.setattr(jobs.reports, "save", save)
    return manager, connect, save


def test_no_database_connection_or_report_until_success(manager):
    manager, connect, save = manager
    request = spec()
    manager.register(request)
    future = manager.submit(request.id)
    job = manager._lookup(request.id)
    assert manager.get_detail(request.id, request.owner_id).status == "queued"
    job.state.update(status="running", progress_pct=50)
    assert manager.get_detail(request.id, request.owner_id).progress_pct == 50
    connect.assert_not_called()
    save.assert_not_called()
    future.set_result(success(job))
    save.assert_called_once()
    assert save.call_args.args[1] == request.owner_id
    assert job.saved


@pytest.mark.parametrize("outcome", ["engine_error", "process_crash", "cancelled", "dispatch_error"])
def test_unsuccessful_execution_never_saves_a_report(manager, outcome):
    manager, connect, save = manager
    request = spec()
    manager.register(request)
    if outcome == "dispatch_error":
        manager._pool.submit.side_effect = RuntimeError("pool closed")
        with pytest.raises(RuntimeError):
            manager.submit(request.id)
    else:
        future = manager.submit(request.id)
        future.set_running_or_notify_cancel()
        if outcome == "engine_error":
            future.set_result({"error": "Invalid strategy"})
        elif outcome == "process_crash":
            future.set_exception(RuntimeError("worker died"))
        else:
            assert manager.cancel(request.id, request.owner_id) == "cancel_requested"
            future.set_result(success(manager._lookup(request.id)))
    connect.assert_not_called()
    save.assert_not_called()
    assert manager.get_detail(request.id, request.owner_id).status == "failed"


def test_report_write_failure_does_not_claim_completion(manager):
    manager, _, save = manager
    save.side_effect = RuntimeError("database unavailable")
    request = spec()
    manager.register(request)
    manager.submit(request.id).set_result(success(manager._lookup(request.id)))
    detail = manager.get_detail(request.id, request.owner_id)
    assert detail.status == "failed"
    assert "database unavailable" in detail.error_message
    assert not manager._lookup(request.id).saved


def test_other_owner_cannot_poll_or_cancel(manager):
    manager, _, _ = manager
    request = spec()
    manager.register(request)
    other = uuid.uuid4()
    assert manager.get_detail(request.id, other) is None
    assert manager.cancel(request.id, other) == "not_found"
    assert not manager._lookup(request.id).state["cancel_requested"]


def test_identical_inputs_are_distinct_executions_and_submit_is_not_duplicated(manager):
    manager, _, save = manager
    first = spec()
    second = spec(first.owner_id)
    for request in (first, second):
        manager.register(request)
        future = manager.submit(request.id)
        assert manager.submit(request.id) is future
        future.set_result(success(manager._lookup(request.id)))
    assert first.id != second.id
    assert save.call_count == 2


def test_finished_transient_errors_expire(manager):
    manager, _, _ = manager
    request = spec()
    manager.register(request)
    manager.submit(request.id).set_result({"error": "failed"})
    manager._lookup(request.id).finished_at = time.monotonic() - jobs.TERMINAL_TTL_SECONDS - 1
    assert manager.get_detail(request.id, request.owner_id) is None


def test_shutdown_cleans_registered_but_undispatched_jobs(manager):
    manager, connect, save = manager
    request = spec()
    manager.register(request)
    job = manager._lookup(request.id)
    ipc = manager._ipc
    ipc.shutdown = Mock()
    manager._pool.shutdown = Mock()
    manager.shutdown(wait=True)
    assert job.finished_at is not None and job.state["status"] == "failed"
    ipc.shutdown.assert_called_once()
    connect.assert_not_called()
    save.assert_not_called()


def test_completed_report_document_has_no_execution_state():
    detail = spec().empty_detail("completed", 100)
    detail.equity_curve = [EquityPoint(date="2025-01-03", equity=100_000)]
    payload = reports.document(detail)
    assert not {"status", "progressPct", "errorMessage"} & payload.keys()
    assert {"metrics", "equityCurve", "trades", "parameters"} <= payload.keys()
    assert list(BacktestReport.__table__.columns.keys()) == [
        "id", "owner_id", "created_at", "strategy_key", "name", "version", "results"]


@pytest.mark.parametrize("state", ["queued", "running", "failed"])
def test_repository_rejects_unfinished_report_before_opening_transaction(state):
    engine = Mock()
    with pytest.raises(ValueError, match="Only successful"):
        reports.save(engine, uuid.uuid4(), spec().empty_detail(state))
    engine.begin.assert_not_called()


def test_history_does_not_list_transient_jobs(monkeypatch):
    from src.services import backtests
    monkeypatch.setattr(backtests, "ensure_schema", Mock(side_effect=AssertionError("no query needed")))
    history = asyncio.run(backtests.list_backtests(owner_id=uuid.uuid4(), status="running"))
    assert history.total == 0 and history.items == []
