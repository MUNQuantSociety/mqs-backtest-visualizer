"""Upload a strategy, have the system prove it by running it, then run it again.

This is the acceptance test for the loop the product owner described: a student
uploads a ``.py`` file, the platform validates it **by executing a real
backtest**, the strategy becomes selectable when that run passes, and from then
on it reruns exactly like a built-in.

The test drives the real FastAPI app through ``TestClient``, so everything is
real: the lifespan, the process pool, the vendored engine, the live
``public.market_data`` table, the strategy store on disk, and the ``app.*``
tables. Nothing about the user-strategy path is mocked, because the parts most
worth testing are the seams — the store round trip, the loader's import, and
the worker hook that flips the registry row.

Two runs of a real backtest is the price of admission, so the uploaded
strategies are deliberately trivial and the validation window is the short one
the pipeline itself uses (thirty days, anchored on the last day of data). The
strategies, their stored source, and every run they produce are removed on the
way out.

Everything here is marked ``db`` and skips cleanly when the database is
unreachable.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from server import app
from src.core.config import settings
from src.db.engine import create_sync_engine, dispose_async_engine
from src.db.init import init_database
from src.integrations.strategy_store import LocalStrategyStore, strategy_key
from src.models import BacktestRun, Strategy
from src.services import strategy_validation

pytestmark = pytest.mark.db

_RUNS = BacktestRun.__table__
_STRATEGIES = Strategy.__table__

# How long a validation run may take before something is wrong. The window is
# thirty calendar days over two tickers; the margin covers a cold parquet cache
# and a slow link to the university network.
RUN_TIMEOUT_SECONDS = 420
POLL_SECONDS = 1.0

TERMINAL_RUN_STATUSES = {"completed", "failed"}
SETTLED_STRATEGY_STATUSES = {"active", "failed_validation"}


# ---------------------------------------------------------------------------
# The uploads themselves
# ---------------------------------------------------------------------------

# A strategy with no indicators and no history lookups: it buys once, sells
# once, and is otherwise the smallest thing that is genuinely a strategy. The
# pipeline is what is under test, not the trading logic.
VALID_SOURCE = '''
"""Minimal uploadable strategy — buys once, sells once."""

import logging

from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class UploadedTestStrategy(BasePortfolio):
    """Trades on a step counter so the run needs no indicator warm-up."""

    def __init__(
        self,
        db_connector,
        executor,
        debug=False,
        config_dict=None,
        backtest_start_date=None,
        order_manager=None,
    ):
        super().__init__(
            db_connector, executor, debug, config_dict, backtest_start_date, order_manager
        )
        self.logger = logging.getLogger(self.__class__.__name__)
        self._steps = 0

    def OnData(self, context):
        self._steps += 1
        ticker = self.tickers[0]
        if self._steps == 2:
            context.buy(ticker, confidence=1.0)
        elif self._steps == 6:
            context.sell(ticker, confidence=1.0)
'''

# Rejected before anything is stored: ``os`` is outside the import allowlist.
SOURCE_IMPORTING_OS = '''
import os

from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class NosyStrategy(BasePortfolio):
    def OnData(self, context):
        os.listdir(".")
'''

# Rejected because there is no answer to "which one runs?".
SOURCE_WITH_TWO_STRATEGIES = '''
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class FirstStrategy(BasePortfolio):
    def OnData(self, context):
        pass


class SecondStrategy(BasePortfolio):
    def OnData(self, context):
        pass
'''

# Passes the scan, imports cleanly, and then throws on the first bar. This is
# the breakage validation exists for: nothing before the event loop can see it,
# so only *running* the strategy proves the strategy runs. The engine swallows
# per-timestamp strategy errors for the batch CLI — one broken portfolio should
# not stop the other eight — and a run of this source used to finish
# ``completed`` with no fills, which activated it.
SOURCE_THAT_RAISES_IN_ONDATA = '''
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class RaisingStrategy(BasePortfolio):
    def OnData(self, context):
        raise ValueError("this strategy raises on every bar")
'''

# Passes the scan and fails the run: the module raises while being imported, in
# the worker, which is exactly the class of breakage validation exists to catch.
SOURCE_THAT_FAILS_TO_IMPORT = '''
from engine.strategies.portfolio_BASE.strategy import BasePortfolio

raise ValueError("this strategy is broken on purpose")


class BrokenStrategy(BasePortfolio):
    def OnData(self, context):
        pass
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine(require_database: None):
    """A sync engine for the test's own bookkeeping.

    ``require_database`` is requested because module-scoped fixtures are
    set up before the function-scoped skip can fire. See tests/conftest.py.
    """

    init_database()
    engine = create_sync_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """The real app, lifespan and worker pool included.

    Used as a context manager rather than bare: entering it runs the lifespan,
    which is what creates the process pool these validation runs execute in,
    and keeps one event loop for the whole module so the asyncpg pool is not
    left bound to a loop that has already closed.
    """
    with TestClient(app) as test_client:
        yield test_client
        test_client.portal.call(dispose_async_engine)


@pytest.fixture(scope="module")
def uploads(db_engine) -> Iterator[list[str]]:
    """Keys created by this module, removed with their runs and stored source."""
    created: list[str] = []
    try:
        yield created
    finally:
        for key in created:
            _forget_strategy(db_engine, key)


def _forget_strategy(db_engine, key: str) -> None:
    """Remove a strategy, its runs, their artifacts, and its stored source."""
    with db_engine.begin() as connection:
        run_ids = [
            row[0]
            for row in connection.execute(
                select(_RUNS.c.id).where(_RUNS.c.strategy_key == key)
            ).all()
        ]
        connection.execute(delete(_RUNS).where(_RUNS.c.strategy_key == key))
        connection.execute(delete(_STRATEGIES).where(_STRATEGIES.c.key == key))

    for run_id in run_ids:
        shutil.rmtree(settings.artifact_dir / str(run_id), ignore_errors=True)
    strategy_validation.discard_stored_source(key)


def _upload(client: TestClient, source: str, name: str):
    """POST the upload form the way the strategy editor will."""
    return client.post(
        "/api/strategies",
        json={
            "name": name,
            "description": "Created and removed by tests/integration/test_user_strategies.py",
            "source": source,
            "filename": "strategy.py",
        },
    )


def _unique_name(label: str) -> str:
    return f"upload {label} {uuid.uuid4().hex[:8]}"


def _strategy_row(db_engine, key: str):
    with db_engine.begin() as connection:
        return connection.execute(
            select(_STRATEGIES).where(_STRATEGIES.c.key == key)
        ).one_or_none()


def _validation_run_id(db_engine, key: str) -> uuid.UUID:
    with db_engine.begin() as connection:
        row = connection.execute(
            select(_RUNS.c.id).where(
                _RUNS.c.strategy_key == key, _RUNS.c.purpose == "validation"
            )
        ).one()
    return row[0]


def _await_strategy_settled(db_engine, key: str, timeout: float):
    """Poll the registry until the validation run has had its say."""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = _strategy_row(db_engine, key)
        assert row is not None, f"strategy {key} disappeared while validating"
        if row.status in SETTLED_STRATEGY_STATUSES:
            return row
        time.sleep(POLL_SECONDS)

    run = _run_row(db_engine, _validation_run_id(db_engine, key))
    pytest.fail(
        f"strategy {key} was still {row.status!r} after {timeout:.0f}s; "
        f"its validation run is {run.status!r} at {run.progress_pct}% "
        f"({run.error_message!r})"
    )


def _run_row(db_engine, run_id: uuid.UUID):
    with db_engine.begin() as connection:
        return connection.execute(select(_RUNS).where(_RUNS.c.id == run_id)).one()


def _poll_run(client: TestClient, run_id: str, timeout: float) -> dict:
    """Poll the detail endpoint the way the frontend does, and return the payload."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/backtests/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in TERMINAL_RUN_STATUSES:
            return body
        time.sleep(POLL_SECONDS)

    pytest.fail(
        f"run {run_id} was {body.get('status')!r} after {timeout:.0f}s; "
        f"progress={body.get('progressPct')} error={body.get('errorMessage')!r}"
    )


@pytest.fixture(scope="module")
def validated_upload(client, db_engine, uploads) -> Iterator[tuple[str, dict]]:
    """One good upload, taken all the way through validation.

    Yields ``(strategy_key, submission_body)`` — the registry key and the 201
    payload the editor would have shown the student.
    """
    name = _unique_name("valid")
    response = _upload(client, VALID_SOURCE, name)
    assert response.status_code == 201, response.text
    body = response.json()
    uploads.append(body["id"])

    _await_strategy_settled(db_engine, body["id"], RUN_TIMEOUT_SECONDS)
    yield body["id"], body


@pytest.fixture(scope="module")
def raising_upload(client, db_engine, uploads) -> Iterator[str]:
    """An upload that imports cleanly and throws inside ``OnData``."""
    name = _unique_name("raising")
    response = _upload(client, SOURCE_THAT_RAISES_IN_ONDATA, name)
    assert response.status_code == 201, response.text
    key = response.json()["id"]
    uploads.append(key)

    _await_strategy_settled(db_engine, key, RUN_TIMEOUT_SECONDS)
    yield key


@pytest.fixture(scope="module")
def broken_upload(client, db_engine, uploads) -> Iterator[str]:
    """An upload that passes the scan and fails when the worker imports it."""
    name = _unique_name("broken")
    response = _upload(client, SOURCE_THAT_FAILS_TO_IMPORT, name)
    assert response.status_code == 201, response.text
    key = response.json()["id"]
    uploads.append(key)

    _await_strategy_settled(db_engine, key, RUN_TIMEOUT_SECONDS)
    yield key


# ---------------------------------------------------------------------------
# Upload: what the student gets back, and what was stored
# ---------------------------------------------------------------------------


def test_upload_answers_immediately_as_a_draft(validated_upload) -> None:
    """Validation takes as long as a backtest; the response must not wait for it."""
    _, body = validated_upload

    # The client's Zod enum knows only active|draft|archived, so an upload that
    # is validating reports ``draft`` and the message carries the real state.
    assert body["status"] == "draft"
    assert "validation" in body["message"].lower()
    assert body["id"].startswith("user-")


def test_the_source_lives_in_the_store_not_in_a_column(
    db_engine, validated_upload
) -> None:
    """The store is the system of record — the staging column is gone for good."""
    key, _ = validated_upload
    row = _strategy_row(db_engine, key)

    assert row.kind == "user"
    assert row.storage_key == strategy_key(key)
    assert row.class_path is None, "an upload is loaded from the store, not imported"
    assert row.source_staging is None

    store = LocalStrategyStore(settings.strategy_store_root)
    assert "BasePortfolio" in store.get(row.storage_key, "strategy.py")
    # Config beside source, because BasePortfolio finds its config by looking
    # next to the file its class was defined in.
    assert "TICKERS" in store.get(row.storage_key, "config.json")


# ---------------------------------------------------------------------------
# Validation is a normal run
# ---------------------------------------------------------------------------


def test_validation_activates_the_strategy(db_engine, validated_upload) -> None:
    """The whole point of validating by running: a passing run flips the row."""
    key, _ = validated_upload
    row = _strategy_row(db_engine, key)

    assert row.status == "active", "a validation run that passed must activate it"
    assert row.enabled is True
    assert row.validation_run_id == _validation_run_id(db_engine, key)


def test_an_activated_strategy_appears_in_the_catalogue(client, validated_upload) -> None:
    key, _ = validated_upload
    listing = client.get("/api/strategies")
    assert listing.status_code == 200

    rows = {item["id"]: item for item in listing.json()["items"]}
    assert key in rows, "an active upload is selectable like any built-in"
    assert rows[key]["status"] == "active"
    assert rows[key]["parameters"], "the run form needs something to render"


def test_the_validation_run_opens_like_any_other_run(
    client, db_engine, validated_upload
) -> None:
    """No separate pipeline: the same detail endpoint serves it."""
    key, _ = validated_upload
    run_id = _validation_run_id(db_engine, key)

    detail = client.get(f"/api/backtests/{run_id}").json()
    assert detail["status"] == "completed", detail.get("errorMessage")
    assert detail["progressPct"] == 100
    assert detail["equityCurve"], "a completed run has an equity curve"
    assert detail["strategyId"] == key


def test_validation_runs_stay_out_of_the_catalogue_aggregates(
    client, validated_upload
) -> None:
    """A student who has never run their strategy should be told exactly that."""
    key, _ = validated_upload
    rows = {item["id"]: item for item in client.get("/api/strategies").json()["items"]}
    assert rows[key]["runCount"] == 0


# ---------------------------------------------------------------------------
# Rerun — the payoff, and the part that needed no new endpoint
# ---------------------------------------------------------------------------


def test_an_activated_strategy_reruns_from_the_catalogue(
    client, db_engine, validated_upload
) -> None:
    """Select it, submit it, get results — the same POST /backtests as a built-in."""
    key, _ = validated_upload
    window = _rerun_window(db_engine, key)

    response = client.post(
        "/api/backtests",
        json={
            "name": "rerun of an uploaded strategy",
            "strategyKey": key,
            "startDate": window["startDate"],
            "endDate": window["endDate"],
            "initialCapital": 100_000,
            "mode": "event",
            "params": {"LOOKBACK_DAYS": 10},
        },
    )
    assert response.status_code == 202, response.text
    accepted = response.json()

    detail = _poll_run(client, accepted["id"], RUN_TIMEOUT_SECONDS)
    assert detail["status"] == "completed", detail.get("errorMessage")
    assert detail["equityCurve"]
    assert detail["parameters"]["LOOKBACK_DAYS"] == 10
    assert detail["metrics"]["totalReturn"] is not None


def _rerun_window(db_engine, key: str) -> dict[str, str]:
    """Reuse the validation run's window, which is known to hold data."""
    run = _run_row(db_engine, _validation_run_id(db_engine, key))
    return {
        "startDate": run.start_date.isoformat(),
        "endDate": run.end_date.isoformat(),
    }


# ---------------------------------------------------------------------------
# Rejected uploads — nothing stored, nothing executed
# ---------------------------------------------------------------------------


def _rejection(response) -> str:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    # A string, not FastAPI's list of error objects: the client's error reader
    # only understands a string and shows a bare HTTP status otherwise.
    assert isinstance(detail, str) and detail
    return detail


def test_source_that_imports_os_is_rejected_by_line(client) -> None:
    """The scan is a speed bump, not a sandbox — but it does have to work."""
    detail = _rejection(_upload(client, SOURCE_IMPORTING_OS, _unique_name("os")))

    # The line number is the point: a student has to be told where to look.
    assert "Line 2" in detail
    assert "'os'" in detail


def test_source_defining_two_strategies_is_rejected(client) -> None:
    detail = _rejection(
        _upload(client, SOURCE_WITH_TWO_STRATEGIES, _unique_name("ambiguous"))
    )
    assert "FirstStrategy" in detail and "SecondStrategy" in detail


def test_source_defining_no_strategy_is_rejected(client) -> None:
    detail = _rejection(_upload(client, "x = 1\n", _unique_name("empty")))
    assert "BasePortfolio" in detail


def test_a_rejected_upload_stores_nothing(client, db_engine) -> None:
    """A 422 must leave no registry row and no object in the store."""
    before = _upload_count(db_engine)
    _rejection(_upload(client, SOURCE_IMPORTING_OS, _unique_name("nothing-stored")))
    assert _upload_count(db_engine) == before


def _upload_count(db_engine) -> int:
    with db_engine.begin() as connection:
        rows = connection.execute(
            select(_STRATEGIES.c.key).where(_STRATEGIES.c.kind == "user")
        ).all()
    return len(rows)


# ---------------------------------------------------------------------------
# An upload that fails its validation run
# ---------------------------------------------------------------------------


def test_a_strategy_that_cannot_be_imported_fails_validation(
    db_engine, broken_upload
) -> None:
    key = broken_upload
    row = _strategy_row(db_engine, key)

    assert row.status == "failed_validation"
    assert row.enabled is False
    # Pointed at the run even in failure: it is where the error message lives,
    # and there is no other way to find it from the catalogue.
    assert row.validation_run_id == _validation_run_id(db_engine, key)


def test_the_failure_reason_is_retrievable_from_the_run(
    client, db_engine, broken_upload
) -> None:
    run_id = _validation_run_id(db_engine, broken_upload)
    detail = client.get(f"/api/backtests/{run_id}").json()

    assert detail["status"] == "failed"
    assert "broken on purpose" in (detail["errorMessage"] or "")


def test_a_failed_upload_cannot_be_run(client, broken_upload) -> None:
    """The rerun path gates on ``enabled``, and says why it is not."""
    response = client.post(
        "/api/backtests",
        json={
            "name": "run of a strategy that failed validation",
            "strategyKey": broken_upload,
            "startDate": "2026-06-01",
            "endDate": "2026-07-01",
            "initialCapital": 100_000,
            "mode": "event",
            "params": {},
        },
    )
    detail = _rejection(response)
    assert "validation" in detail.lower()


def test_a_failed_upload_stays_out_of_the_catalogue(client, broken_upload) -> None:
    keys = {item["id"] for item in client.get("/api/strategies").json()["items"]}
    assert broken_upload not in keys


def test_a_strategy_that_raises_while_trading_fails_validation(
    db_engine, raising_upload
) -> None:
    """The gate's real job: reject a strategy whose trading logic never works.

    Nothing before the event loop can tell this source from a working one — it
    scans clean, it imports, its class is found. Only the run knows, and only
    because the runner refuses to swallow a strategy exception when the caller
    asked for a verdict.
    """
    row = _strategy_row(db_engine, raising_upload)

    assert row.status == "failed_validation", (
        "a strategy that throws on every bar must not be activated"
    )
    assert row.enabled is False


def test_the_raised_error_is_retrievable_from_the_run(
    client, db_engine, raising_upload
) -> None:
    """The student has to be able to read what their code did wrong."""
    run_id = _validation_run_id(db_engine, raising_upload)
    detail = client.get(f"/api/backtests/{run_id}").json()

    assert detail["status"] == "failed"
    assert "raises on every bar" in (detail["errorMessage"] or "")


# ---------------------------------------------------------------------------
# The loader, on its own
# ---------------------------------------------------------------------------


def test_the_loader_produces_a_path_run_single_can_import(tmp_path: Path) -> None:
    """The seam that lets an upload travel through the unmodified run pipeline.

    The loader registers the imported module in ``sys.modules`` under a
    synthetic name, so ``run_single``'s ordinary ``importlib.import_module``
    resolves it without ever knowing the class came off disk.
    """
    from engine.run_single import load_strategy_class
    from engine.strategies.user_loader import load_user_strategy

    store = LocalStrategyStore(tmp_path / "store")
    key = strategy_key("loader-check")
    store.put(key, "strategy.py", VALID_SOURCE)
    store.put(key, "config.json", "{}")

    loaded = load_user_strategy(
        storage_key=key,
        store=store,
        dest_dir=tmp_path / "work",
        token=uuid.uuid4().hex,
    )

    assert loaded.strategy_class.__name__ == "UploadedTestStrategy"
    assert load_strategy_class(loaded.class_path) is loaded.strategy_class
    # config.json must land beside the source: BasePortfolio finds its config
    # by looking next to the file its class was defined in.
    assert (loaded.directory / "config.json").is_file()


def test_the_loader_refuses_a_file_with_two_strategies(tmp_path: Path) -> None:
    from engine.strategies.user_loader import UserStrategyError, load_user_strategy

    store = LocalStrategyStore(tmp_path / "store")
    key = strategy_key("loader-ambiguous")
    store.put(key, "strategy.py", SOURCE_WITH_TWO_STRATEGIES)
    store.put(key, "config.json", "{}")

    with pytest.raises(UserStrategyError) as excinfo:
        load_user_strategy(
            storage_key=key,
            store=store,
            dest_dir=tmp_path / "work",
            token=uuid.uuid4().hex,
        )
    assert "more than one" in str(excinfo.value)
