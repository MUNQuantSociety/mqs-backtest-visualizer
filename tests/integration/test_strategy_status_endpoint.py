"""Watching an upload from the outside, the way a client has to.

The catalogue hides anything that is not active, so until now the only way to
follow a submission was to read the database. These tests drive the full loop
over HTTP alone — file in, ``validationRunId`` out, ``GET /strategies/{key}``
while it validates, the run's own detail, and the flip to ``active`` — and
never touch a table directly.

    pytest tests/integration/test_strategy_status_endpoint.py -v
"""

import time
import uuid
from collections.abc import Iterator
from functools import partial

import pytest
from fastapi.testclient import TestClient

from server import app
from src.db.engine import dispose_async_engine
from src.services import backtests as backtests_service
from src.services import strategies as strategies_service
from src.services.strategy_validation.template import STARTER_SOURCE

POLL_SECONDS = 2.0
RUN_TIMEOUT_SECONDS = 300.0
TERMINAL = {"completed", "failed"}

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def client(database_available: tuple[bool, str]) -> Iterator[TestClient]:
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)
    with TestClient(app) as test_client:
        yield test_client
        test_client.portal.call(dispose_async_engine)


@pytest.fixture(scope="module")
def uploaded(client: TestClient) -> Iterator[dict]:
    """One template upload sent as a real multipart file; removed afterwards."""
    response = client.post(
        "/api/strategies/upload",
        files={"file": ("strategy.py", STARTER_SOURCE.encode(), "text/x-python")},
        data={
            "name": f"status endpoint {uuid.uuid4().hex[:8]}",
            "description": "created and removed by test_strategy_status_endpoint.py",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    try:
        yield body
    finally:
        # Runs first, then the strategy row and its stored source.
        if body.get("validationRunId"):
            client.portal.call(
                partial(backtests_service.delete_backtest, body["validationRunId"])
            )
        client.portal.call(partial(strategies_service.delete_strategy, body["id"]))


def _poll_run(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/backtests/{run_id}").json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(POLL_SECONDS)
    pytest.fail(f"run {run_id} still {body.get('status')!r} after {RUN_TIMEOUT_SECONDS:.0f}s")


def test_the_submission_carries_the_run_to_poll(uploaded: dict) -> None:
    assert uploaded["status"] == "draft"
    run_id = uploaded["validationRunId"]
    assert run_id, "a queued validation must hand back its run id as a field"
    uuid.UUID(run_id)  # a real id, not the prose sentence it also appears in
    assert run_id in uploaded["message"]


def test_a_validating_upload_is_readable_by_key_but_not_listed(
    client: TestClient, uploaded: dict
) -> None:
    key = uploaded["id"]

    by_key = client.get(f"/api/strategies/{key}")
    assert by_key.status_code == 200
    body = by_key.json()
    # Real lifecycle in the additive field, client enum in the old one.
    assert body["validationState"] in {"validating", "active", "failed_validation"}
    assert body["validationRunId"] == uploaded["validationRunId"]
    assert body["status"] in {"draft", "active"}

    if body["validationState"] == "validating":
        listed = {item["id"] for item in client.get("/api/strategies").json()["items"]}
        assert key not in listed, "a validating upload must stay out of the catalogue"


def test_the_validation_run_finishes_and_the_strategy_activates(
    client: TestClient, uploaded: dict
) -> None:
    run = _poll_run(client, uploaded["validationRunId"])
    assert run["status"] == "completed", run.get("errorMessage")
    assert run["equityCurve"], "a completed validation run has a curve like any run"

    # The worker writes the outcome after the run row goes terminal; allow it a
    # moment rather than asserting on the same instant.
    deadline = time.monotonic() + 30
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/strategies/{uploaded['id']}").json()
        if body["validationState"] == "active":
            break
        time.sleep(1)

    assert body["validationState"] == "active"
    assert body["status"] == "active"
    listed = {item["id"] for item in client.get("/api/strategies").json()["items"]}
    assert uploaded["id"] in listed, "an activated upload joins the catalogue"


def test_an_unknown_key_is_a_404_with_a_message(client: TestClient) -> None:
    response = client.get("/api/strategies/user-does-not-exist-00000000")
    assert response.status_code == 404
    assert response.json()["detail"]
