import asyncio
import io
import logging
from uuid import uuid4

from src.core import logging_config
from src.services import strategy_availability
from src.workers import run_job as worker


def test_console_logging_survives_existing_root_handlers_and_is_idempotent(monkeypatch):
    root = logging.Logger("isolated", level=logging.WARNING)
    root.addHandler(logging.NullHandler())
    output = io.StringIO()
    monkeypatch.setattr(logging_config.logging, "getLogger", lambda: root)
    monkeypatch.setattr(logging_config.sys, "stdout", output)
    logging_config.configure_logging("INFO")
    logging_config.configure_logging("INFO")
    root.info("READY | visible")
    assert output.getvalue().count("READY | visible") == 1
    assert "pid=" in output.getvalue()
    assert len([handler for handler in root.handlers if handler.name == "mqs.console"]) == 1


def test_progress_stages_are_info_and_duplicate_callbacks_are_quiet(monkeypatch, caplog):
    run_id = uuid4()
    heartbeat = worker._RunHeartbeat(None, run_id)
    monkeypatch.setattr(heartbeat, "_poll", lambda: None)
    with caplog.at_level(logging.INFO):
        for pct, stage in [(0, "starting"), (0, "starting"), (0, "loading data"), (10, "simulating"), (100, "writing report")]:
            heartbeat.on_progress(pct, stage)
    messages = [record.message for record in caplog.records if "PROGRESS |" in record.message]
    assert len(messages) == 4
    assert all(str(run_id) in message for message in messages)
    assert any("stage=loading data" in message for message in messages)
    assert any("progress=100%" in message for message in messages)


def test_storage_logs_steps_not_source_or_config_contents(monkeypatch, caplog):
    class Store:
        def get(self, key, filename):
            return "private-source-and-config-sentinel"

    monkeypatch.setattr(strategy_availability, "get_strategy_store", Store)
    with caplog.at_level(logging.INFO):
        assert asyncio.run(strategy_availability.package_available("strategies/portfolio_1/"))
    assert "Checking source/config" in caplog.text
    assert "Source/config present" in caplog.text
    assert "private-source-and-config-sentinel" not in caplog.text


def test_http_arrival_and_response_are_correlated_without_logging_secrets(caplog):
    from fastapi.testclient import TestClient
    from server import app

    # No context manager: a health request needs no live database lifespan.
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="src.api.requests"):
        response = client.get("/api/health?token=private-query-sentinel", headers={
            "Authorization": "Bearer private-header-sentinel",
        })
    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    messages = [record.message for record in caplog.records if record.name == "src.api.requests"]
    assert len(messages) == 2
    assert all(f"request={request_id}" in message for message in messages)
    assert "HTTP IN" in messages[0]
    assert "status=200" in messages[1]
    assert "elapsed_ms=" in messages[1]
    assert all("private-" not in message for message in messages)


def test_http_rejection_is_visible_as_warning(caplog):
    from fastapi.testclient import TestClient
    from server import app

    with caplog.at_level(logging.INFO, logger="src.api.requests"):
        response = TestClient(app).post("/api/backtests", json={})
    assert response.status_code == 422
    assert any(record.levelno == logging.WARNING and "status=422" in record.message
               for record in caplog.records if record.name == "src.api.requests")


def test_execution_costs_are_not_rounded_to_zero_in_logs(caplog):
    from engine.core.executor import BacktestExecutor

    with caplog.at_level(logging.INFO):
        BacktestExecutor(200_000, ["CRWV", "NBIS"], slippage=0.0005,
                         commission_per_share=0.005)
    assert "slippage=0.000500" in caplog.text
    assert "commission_per_share=0.005000" in caplog.text
