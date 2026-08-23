"""``POST /backtests`` end to end — the Run Backtest button, over HTTP.

This is the acceptance test for the endpoint the whole application exists to
offer. It drives the real FastAPI app through ``TestClient``, which means the
real lifespan, the real process pool, the real vendored engine, the live
``public.market_data`` table, and the real ``app.*`` tables. A run submitted
here is a run: it queues, claims a worker, reports progress, and lands with
metrics, an equity curve, and trades that the detail endpoint serves.

Two things make that affordable. The successful run happens once, in a
module-scoped fixture, and every assertion about it reads the payload that run
produced. And the window is pinned short and inside verified data coverage, so
it finishes in well under a minute rather than in the minutes a student's
five-year backtest would take.

Everything is marked ``db`` and skips cleanly when the database is unreachable.
The strategies it needs are created and removed by the test, so it does not
depend on ``scripts/seed_strategies.py`` having been run, and nothing it writes
survives the module.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select

from server import app
from src.core.config import settings
from src.db.engine import create_sync_engine, dispose_async_engine
from src.db.init import init_database
from src.models import BacktestRun, Strategy
from src.schemas.backtests import BacktestSummary
from src.workers.run_job import CANCELLED_MESSAGE

pytestmark = pytest.mark.db

_RUNS = BacktestRun.__table__
_STRATEGIES = Strategy.__table__

DUMMY_CLASS_PATH = "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy"

# Pinned inside verified coverage: ``market_data`` runs from 2019-11-11 to
# 2026-07-15, so a window computed from today's date returns no rows and would
# make a healthy pipeline look broken. Long enough that the strategy opens
# *and closes* positions — a window with only entries in it would leave the
# round-trip half of the trade table untested.
WINDOW_START = "2026-03-02"
WINDOW_END = "2026-07-15"

# The parameter spec the throwaway strategies advertise. Its bounds are what
# the out-of-range test below is checked against, so they live in one place.
LOOKBACK_SPEC = {
    "key": "LOOKBACK_DAYS",
    "label": "Lookback (days)",
    "type": "integer",
    "default": 50,
    "min": 5,
    "max": 365,
}

# How long a pinned run may take before something is wrong. Measured at roughly
# 25 seconds wall clock; the margin covers a cold parquet cache and a slow link
# to the university network.
RUN_TIMEOUT_SECONDS = 420
POLL_SECONDS = 1.0

TERMINAL = {"completed", "failed"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine(database_available: tuple[bool, str]):
    """A sync engine for the test's own bookkeeping.

    ``database_available`` is requested explicitly because module-scoped
    fixtures are set up before the function-scoped ``db``-marker skip: without
    it, an offline machine would fail to connect here instead of skipping.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)

    init_database()
    engine = create_sync_engine()
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_strategy(engine, key: str, *, enabled: bool, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(_STRATEGIES).values(
                key=key,
                name=f"Run submission fixture ({status})",
                description="Created and removed by tests/integration/test_run_submission.py",
                tags=["test"],
                universe=["AAPL", "TSLA", "NVDA", "MSFT"],
                param_specs=[LOOKBACK_SPEC],
                kind="builtin",
                class_path=DUMMY_CLASS_PATH,
                status=status,
                enabled=enabled,
            )
        )


@pytest.fixture(scope="module")
def runnable_key(db_engine) -> Iterator[str]:
    """An enabled registry row pointing at the vendored dummy strategy."""
    key = f"test-submit-{uuid.uuid4().hex[:8]}"
    _insert_strategy(db_engine, key, enabled=True, status="active")
    try:
        yield key
    finally:
        with db_engine.begin() as connection:
            connection.execute(delete(_RUNS).where(_RUNS.c.strategy_key == key))
            connection.execute(delete(_STRATEGIES).where(_STRATEGIES.c.key == key))


@pytest.fixture(scope="module")
def validating_key(db_engine) -> Iterator[str]:
    """A disabled row, in the state an upload sits in while it is validated."""
    key = f"test-draft-{uuid.uuid4().hex[:8]}"
    _insert_strategy(db_engine, key, enabled=False, status="validating")
    try:
        yield key
    finally:
        with db_engine.begin() as connection:
            connection.execute(delete(_STRATEGIES).where(_STRATEGIES.c.key == key))


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """The real app, lifespan and worker pool included.

    ``TestClient`` used as a context manager rather than bare: entering it runs
    the lifespan (which is what creates the process pool that executes these
    runs at all) and keeps one event loop for the whole module, so the asyncpg
    pool is not left bound to a loop that has already closed.
    """
    with TestClient(app) as test_client:
        yield test_client
        test_client.portal.call(dispose_async_engine)


def _submit(client: TestClient, strategy_key: str, **overrides) -> dict:
    """POST a run the way the New Run form will, and return the raw response."""
    payload = {
        "name": "run submission integration",
        "strategyKey": strategy_key,
        "startDate": WINDOW_START,
        "endDate": WINDOW_END,
        "initialCapital": 100_000,
        "mode": "event",
        "params": {"LOOKBACK_DAYS": 5},
    }
    payload.update(overrides)
    return client.post("/api/backtests", json=payload)


def _poll_until(client: TestClient, run_id: str, wanted: set[str], timeout: float) -> dict:
    """Poll the detail endpoint the way the frontend does, and return the payload.

    Fails with the run's own error message rather than a bare timeout: a run
    that failed for a reason the test did not expect should say what it was.
    """
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/backtests/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in wanted:
            return body
        time.sleep(POLL_SECONDS)

    pytest.fail(
        f"run {run_id} was {body.get('status')!r} after {timeout:.0f}s, "
        f"waiting for one of {sorted(wanted)}; "
        f"progress={body.get('progressPct')} error={body.get('errorMessage')!r}"
    )


def _delete_run(engine, run_id: str) -> None:
    """Teardown: remove the row and the engine's CSVs, whatever state it is in."""
    with engine.begin() as connection:
        connection.execute(delete(_RUNS).where(_RUNS.c.id == uuid.UUID(run_id)))
    shutil.rmtree(settings.artifact_dir / run_id, ignore_errors=True)


@pytest.fixture(scope="module")
def completed_run(
    client: TestClient, db_engine, runnable_key: str
) -> Iterator[tuple[dict, dict]]:
    """One real backtest, submitted over HTTP and polled to a terminal state.

    Yields ``(accepted_body, detail_body)`` — the 202 payload the client would
    have cached and the detail payload it would have polled its way to.
    """
    response = _submit(client, runnable_key)
    assert response.status_code == 202, response.text
    accepted = response.json()

    try:
        detail = _poll_until(client, accepted["id"], TERMINAL, RUN_TIMEOUT_SECONDS)
        yield accepted, detail
    finally:
        _delete_run(db_engine, accepted["id"])


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_submission_is_accepted_as_a_list_row(completed_run) -> None:
    """202 carries the whole summary, so the client can cache it immediately."""
    accepted, _ = completed_run

    expected = {
        field.alias or name for name, field in BacktestSummary.model_fields.items()
    }
    assert set(accepted) == expected
    assert accepted["strategyId"]
    assert accepted["timeframe"] == "1d"
    # Four tickers in the fixture's universe, and one symbol column to say so.
    assert accepted["symbol"] == "MULTI"
    assert accepted["initialCapital"] == 100_000
    assert accepted["startDate"] == WINDOW_START
    assert accepted["endDate"] == WINDOW_END


def test_the_run_completes_with_results(completed_run) -> None:
    """The whole point: submit a run, poll, get numbers back."""
    _, detail = completed_run

    assert detail["status"] == "completed", detail.get("errorMessage")
    assert detail["errorMessage"] is None
    assert detail["progressPct"] == 100

    assert detail["equityCurve"], "a completed run must have an equity curve"
    assert detail["trades"], "the pinned window should produce trades"

    metrics = detail["metrics"]
    assert metrics["totalTrades"] > 0
    # The engine reports these; zeros for all of them would mean the metrics
    # row was never written and the serialiser fell back to its placeholder.
    assert any(
        metrics[key] != 0
        for key in ("totalReturn", "sharpe", "maxDrawdown", "volatility")
    )


def test_headline_numbers_reach_the_list_row(client, completed_run) -> None:
    """The list view reads only the run row, so the run row must be filled in."""
    accepted, detail = completed_run

    listing = client.get("/api/backtests", params={"strategyId": accepted["strategyId"]})
    assert listing.status_code == 200

    rows = {row["id"]: row for row in listing.json()["items"]}
    row = rows[accepted["id"]]

    assert row["status"] == "completed"
    assert row["finalEquity"] == pytest.approx(detail["equityCurve"][-1]["equity"])
    assert row["totalReturn"] == pytest.approx(detail["metrics"]["totalReturn"])
    assert row["sharpe"] == pytest.approx(detail["metrics"]["sharpe"])


def test_submitted_parameters_come_back_on_the_run(completed_run) -> None:
    """``parameters`` is the overlay the engine actually ran with."""
    _, detail = completed_run

    assert detail["parameters"]["LOOKBACK_DAYS"] == 5
    # The execution mode rides in the same column: there is no mode column, and
    # a lower-case key cannot collide with a config.json parameter.
    assert detail["parameters"]["mode"] == "event"


# ---------------------------------------------------------------------------
# Rejections — every one of them a sentence a student can act on
# ---------------------------------------------------------------------------


def _rejection(response) -> str:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    # A string, not FastAPI's list of error objects: the client's error reader
    # only understands a string and shows a bare HTTP status otherwise.
    assert isinstance(detail, str) and detail
    return detail


def test_an_unknown_strategy_is_rejected_by_name(client) -> None:
    detail = _rejection(_submit(client, "portfolio_does_not_exist"))
    assert "portfolio_does_not_exist" in detail


def test_a_strategy_still_validating_is_rejected_with_its_reason(
    client, validating_key
) -> None:
    """Disabled is the gate; *why* it is disabled is what the student needs."""
    detail = _rejection(_submit(client, validating_key))
    assert "validat" in detail.lower()


def test_a_backwards_window_is_rejected(client, runnable_key) -> None:
    detail = _rejection(
        _submit(client, runnable_key, startDate=WINDOW_END, endDate=WINDOW_START)
    )
    assert "startDate" in detail and "endDate" in detail


def test_a_malformed_date_is_rejected(client, runnable_key) -> None:
    detail = _rejection(_submit(client, runnable_key, startDate="02/03/2026"))
    assert "startDate" in detail


def test_a_window_past_the_maximum_is_rejected(client, runnable_key) -> None:
    detail = _rejection(
        _submit(client, runnable_key, startDate="1990-01-01", endDate="2026-07-15")
    )
    assert str(settings.max_backtest_window_days) in detail


def test_zero_capital_is_rejected(client, runnable_key) -> None:
    detail = _rejection(_submit(client, runnable_key, initialCapital=0))
    assert "initialCapital" in detail


def test_an_unnamed_run_is_rejected(client, runnable_key) -> None:
    assert _rejection(_submit(client, runnable_key, name="   "))


def test_an_unknown_parameter_is_rejected_by_key(client, runnable_key) -> None:
    detail = _rejection(_submit(client, runnable_key, params={"LOOKBAK_DAYS": 30}))
    assert "LOOKBAK_DAYS" in detail
    # ...and the message says what would have worked.
    assert "LOOKBACK_DAYS" in detail


def test_a_parameter_below_its_minimum_is_rejected(client, runnable_key) -> None:
    detail = _rejection(
        _submit(client, runnable_key, params={"LOOKBACK_DAYS": LOOKBACK_SPEC["min"] - 1})
    )
    assert "LOOKBACK_DAYS" in detail
    assert str(LOOKBACK_SPEC["min"]) in detail


def test_a_parameter_above_its_maximum_is_rejected(client, runnable_key) -> None:
    detail = _rejection(
        _submit(client, runnable_key, params={"LOOKBACK_DAYS": LOOKBACK_SPEC["max"] + 1})
    )
    assert str(LOOKBACK_SPEC["max"]) in detail


def test_a_fractional_integer_parameter_is_rejected(client, runnable_key) -> None:
    detail = _rejection(_submit(client, runnable_key, params={"LOOKBACK_DAYS": 30.5}))
    assert "whole number" in detail


def test_a_parameter_named_mode_is_rejected_rather_than_overwritten(
    client, runnable_key
) -> None:
    """The stored overlay carries the execution mode under this exact key.

    Accepting it as a parameter would mean validating a value and then writing
    the run's mode straight over it — the strategy would silently never see it.
    """
    detail = _rejection(_submit(client, runnable_key, params={"mode": "whatever"}))
    assert "mode" in detail
    assert "reserved" in detail.lower()


def test_an_unknown_mode_is_rejected(client, runnable_key) -> None:
    detail = _rejection(_submit(client, runnable_key, mode="turbo"))
    assert "mode" in detail


def test_nothing_is_persisted_when_a_submission_is_rejected(
    client, db_engine, runnable_key
) -> None:
    """A 422 must not leave a row behind for the student to wonder about."""
    with db_engine.begin() as connection:
        before = connection.execute(
            select(_RUNS.c.id).where(_RUNS.c.strategy_key == runnable_key)
        ).all()

    _rejection(_submit(client, runnable_key, params={"NOT_A_PARAM": 1}))

    with db_engine.begin() as connection:
        after = connection.execute(
            select(_RUNS.c.id).where(_RUNS.c.strategy_key == runnable_key)
        ).all()
    assert after == before


# ---------------------------------------------------------------------------
# Cancellation and deletion
# ---------------------------------------------------------------------------


def test_deleting_a_running_run_cancels_it_then_deleting_again_removes_it(
    client, db_engine, runnable_key
) -> None:
    """Both halves of DELETE's contract, in the order a student meets them.

    The first DELETE lands on a run a worker owns, so it cannot remove the row
    — the worker needs it to record why it stopped. It requests cancellation
    and answers 204; the run terminates on its own as ``failed`` with the
    message the frontend was told to expect. The second DELETE finds a terminal
    run and removes it, and the engine's artifact directory with it.
    """
    response = _submit(client, runnable_key)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]

    try:
        # Cancelling while queued would delete the row outright (nothing has
        # claimed it), which is the other branch entirely.
        state = _poll_until(client, run_id, {"running"} | TERMINAL, RUN_TIMEOUT_SECONDS)
        assert state["status"] == "running", (
            "the pinned window is meant to take long enough to cancel mid-run; "
            f"this one was already {state['status']!r}"
        )

        assert client.delete(f"/api/backtests/{run_id}").status_code == 204

        terminal = _poll_until(client, run_id, TERMINAL, RUN_TIMEOUT_SECONDS)
        assert terminal["status"] == "failed"
        assert terminal["errorMessage"] == CANCELLED_MESSAGE

        artifact_dir = settings.artifact_dir / run_id
        assert client.delete(f"/api/backtests/{run_id}").status_code == 204
        assert client.get(f"/api/backtests/{run_id}").status_code == 404
        assert not artifact_dir.exists(), "deleting a run removes its artifacts"
    finally:
        _delete_run(db_engine, run_id)


def test_deleting_a_queued_run_removes_it(client, db_engine, runnable_key) -> None:
    """A run no worker has claimed is deleted, not cancelled.

    Nothing would ever act on the flag, so the row would sit in ``queued``
    forever while the client — which dropped it from its cache the moment it
    asked — watched it reappear on the next refetch.
    """
    # Submitted with the pool already busy is not something a test can arrange
    # reliably, so this races the worker deliberately: whichever branch the
    # delete takes, the run must not survive as a queued row.
    response = _submit(client, runnable_key)
    assert response.status_code == 202
    run_id = response.json()["id"]

    try:
        assert client.delete(f"/api/backtests/{run_id}").status_code == 204
        final = client.get(f"/api/backtests/{run_id}")
        if final.status_code == 200:
            # It was claimed first; then it must be cancelling, not queued.
            assert final.json()["status"] != "queued"
            _poll_until(client, run_id, TERMINAL, RUN_TIMEOUT_SECONDS)
        else:
            assert final.status_code == 404
    finally:
        _delete_run(db_engine, run_id)


# ---------------------------------------------------------------------------
# The dispatch failure the student would otherwise never see
# ---------------------------------------------------------------------------


def test_a_refused_dispatch_fails_the_run_instead_of_stranding_it(
    client, db_engine, runnable_key, monkeypatch
) -> None:
    """A pool that will not take the job must not leave the row ``queued``.

    Queued-forever is indistinguishable from queued-behind-someone-else in the
    UI, so the run is marked failed with the reason and reported that way in
    the 202 the client caches.
    """
    from src.workers import job_manager

    class _BrokenManager:
        def submit(self, run_id):
            raise RuntimeError("the pool is shut down")

    monkeypatch.setattr(job_manager, "get_job_manager", lambda: _BrokenManager())

    response = _submit(client, runnable_key)
    assert response.status_code == 202, response.text
    body = response.json()
    run_id = body["id"]

    try:
        assert body["status"] == "failed"

        detail = client.get(f"/api/backtests/{run_id}").json()
        assert detail["status"] == "failed"
        assert "shut down" in detail["errorMessage"]
    finally:
        _delete_run(db_engine, run_id)
