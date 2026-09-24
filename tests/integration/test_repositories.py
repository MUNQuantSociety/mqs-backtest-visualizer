"""Repository tests against the live MQS PostgreSQL.

Everything here is marked ``db`` and skips cleanly when the database is
unreachable (see ``tests/conftest.py``). These are the only tests that write to
``app.*``; each one removes what it created, so a rerun starts from the same
state and the shared database never accumulates test litter.

They cover what the contract tests structurally cannot: that the schema and the
seeded registry are really there. Run and report persistence lives in
``tests/unit/test_completed_reports.py`` and ``test_ci_pipeline.py``: since
reports are saved only on success, no run row exists to round-trip here.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.db.engine import (
    detached_async_engine,
    dispose_async_engine,
    session_scope,
)
from src.db.init import init_database
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.services import backtests as backtests_service
from src.services import strategies as strategies_service

pytestmark = pytest.mark.db

SEEDED_KEYS = {"portfolio_1", "portfolio_2", "portfolio_3", "portfolio_dummy"}


def _run(coro):
    """Run one coroutine to completion on an engine belonging to this call.

    The suite has no async plugin, so tests stay synchronous and reach into the
    event loop here. One loop per call, and — via ``detached_async_engine`` —
    one engine per call, disposed on the way out: the pool's connections and
    the loop that owns them share a lifetime, and no other test module's engine
    is ever within reach of that dispose. ``tests/unit/test_api_contract.py``
    holds a module-scoped client with a pool of its own, and it must survive
    however pytest orders the files.
    """

    async def _main():
        try:
            return await coro
        finally:
            await dispose_async_engine()

    with detached_async_engine():
        return asyncio.run(_main())


@pytest.fixture(scope="module", autouse=True)
def schema(database_available: tuple[bool, str]) -> None:
    """Make sure the app schema exists before anything queries it.

    The reachability check is repeated here rather than left to the ``db``
    marker: this fixture is module-scoped, so it runs *before* the marker's
    function-scoped skip and would otherwise fail to connect on an offline
    machine instead of skipping.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)
    init_database()


def test_schema_creation_is_idempotent() -> None:
    # Running it twice is the normal case: every worker process and every
    # script calls it on startup.
    init_database()
    init_database()


def test_seeded_strategies_are_present_and_enabled_correctly() -> None:
    async def scenario():
        async with session_scope() as session:
            everything = await strategies_repo.list_strategies(
                session, include_disabled=True
            )
            enabled = await strategies_repo.list_strategies(session)
        return everything, enabled

    everything, enabled = _run(scenario())

    keys = {row.strategy.key for row in everything}
    assert SEEDED_KEYS <= keys, "run scripts/seed_strategies.py first"

    enabled_keys = {row.strategy.key for row in enabled}
    assert {"portfolio_1", "portfolio_2", "portfolio_3"} <= enabled_keys
    assert "portfolio_dummy" not in enabled_keys

    builtin = next(row for row in everything if row.strategy.key == "portfolio_1")
    assert builtin.strategy.kind == "builtin"
    assert builtin.strategy.class_path
    assert builtin.strategy.universe
    # param_specs must satisfy the frontend's ParameterSpec shape.
    for spec in builtin.strategy.param_specs:
        assert set(spec) >= {"key", "label", "type", "default"}
        assert spec["type"] in {"number", "integer", "percent", "boolean"}


def test_unknown_run_id_is_none_not_an_error() -> None:
    # The route turns this into a 404; a non-UUID path segment must not become
    # a 500 on the way there.
    assert runs_repo.parse_run_id("does-not-exist") is None
    assert runs_repo.parse_run_id(str(uuid.uuid4())) is not None
    assert _run(backtests_service.get_backtest("does-not-exist")) is None
    assert _run(backtests_service.get_backtest(str(uuid.uuid4()))) is None


def test_user_strategy_submission_refuses_source_it_could_not_run() -> None:
    """An upload is now scanned before it is stored, and stored before it runs.

    Submitting starts a real validation backtest, so a file that is not a
    strategy is refused outright and *nothing* is written — no store object, no
    registry row, nothing to clean up. The accepted half of this path needs the
    worker pool and lives in ``tests/integration/test_user_strategies.py``.
    """
    from src.schemas.strategies import StrategySubmission
    from src.services.strategy_validation import StrategyValidationError

    submission = StrategySubmission(
        name="Integration upload",
        description="written by the test suite",
        source="# this file defines no strategy\n",
        filename="upload.py",
    )

    async def scenario():
        with pytest.raises(StrategyValidationError) as excinfo:
            await strategies_service.submit_strategy(submission)

        async with session_scope() as session:
            rows = await strategies_repo.list_strategies(
                session, include_disabled=True
            )
        return str(excinfo.value), {row.strategy.key for row in rows}

    message, keys = _run(scenario())

    assert "BasePortfolio" in message
    assert not any(key.startswith("user-integration-upload-") for key in keys)
