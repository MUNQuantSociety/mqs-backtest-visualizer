"""Contract tests for the endpoints the frontend calls.

The client parses every response with Zod, so a renamed or snake_case key is a
hard failure in the browser, not a cosmetic difference. These tests assert the
shape the frontend expects — they are the cheap early warning for that.

    pytest tests/unit/test_api_contract.py -v

Two things to know before editing:

* **The backtest group now reads PostgreSQL.** Those tests carry ``@pytest.mark.db``
  and skip cleanly when the database is unreachable. Where the payload shape used
  to be asserted against generated sample rows, it is now asserted against the
  Pydantic models themselves, so the contract stays covered on an offline laptop
  where no row exists to inspect.
* **An empty list is a correct answer.** Until the run pipeline writes its first
  row, ``GET /backtests`` returns ``[]``. The tests assert the envelope and the
  key names, never that rows exist.

``/live/*`` still serves sample data and needs no database.
"""

from collections.abc import Iterator
from datetime import date
from functools import partial

import pytest
from fastapi.testclient import TestClient

from server import app
from src.db.engine import dispose_async_engine
from src.services import backtests as backtests_service
from src.schemas.backtests import (
    BacktestDetail,
    BacktestRunRequest,
    BacktestSummary,
    EquityPoint,
    PerformanceMetrics,
    Trade,
)
from src.schemas.strategies import (
    CompatibilityIssue,
    Strategy,
    StrategyCheckResult,
)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """A client whose requests all share one event loop.

    ``TestClient`` outside a ``with`` block spins up a fresh event loop per
    request; the asyncpg pool's connections belong to the loop that opened them,
    so the second request would find a pool bound to a closed loop. Entering the
    context manager keeps one loop for the whole module.
    """
    with TestClient(app) as test_client:
        yield test_client
        # Close the pool inside that loop, before it goes away.
        test_client.portal.call(dispose_async_engine)


# The disabled test harness, deliberately: a run against it can never be
# confused with a student's work in the shared database.
CONTRACT_RUN_STRATEGY = "portfolio_dummy"


@pytest.fixture(scope="module")
def seeded_run(
    client: TestClient, database_available: tuple[bool, str]
) -> Iterator[BacktestSummary]:
    """One real run row for the duration of this module.

    Without it the list assertions below iterate an empty collection and pass
    without testing anything — the shared database starts with no runs, and the
    submission endpoint that would create one is a later task. The row is
    created through the service (not raw SQL) so the serialisation the client
    parses is the thing under test, and removed afterwards so the shared
    database keeps no test litter.

    Everything goes through ``client.portal`` to stay on the event loop that
    owns the connection pool.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)

    summary = client.portal.call(
        partial(
            backtests_service.create_backtest_run,
            name="contract test run",
            strategy_key=CONTRACT_RUN_STRATEGY,
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 31),
            initial_capital=100_000,
            symbol="MULTI",
            engine_version="test",
            params={"LOOKBACK_DAYS": 30},
        )
    )
    try:
        yield summary
    finally:
        client.portal.call(partial(backtests_service.delete_backtest, summary.id))


def _aliases(model: type) -> set[str]:
    """The wire-level key names of a Pydantic model — i.e. what Zod sees."""
    return {
        field.alias or name for name, field in model.model_fields.items()
    }


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Payload shapes, asserted against the models rather than against live rows.
# These need no database and no runs, so they keep covering the contract even
# when nothing has been backtested yet.
# ---------------------------------------------------------------------------


def test_backtest_summary_keys_are_camel_case() -> None:
    assert _aliases(BacktestSummary) == {
        "id", "name", "strategyId", "strategyName", "symbol", "timeframe",
        "status", "startDate", "endDate", "createdAt", "initialCapital",
        "finalEquity", "totalReturn", "sharpe", "maxDrawdown",
    }


def test_backtest_detail_carries_curve_trades_and_metrics() -> None:
    assert _aliases(BacktestDetail) >= _aliases(BacktestSummary)
    assert {"metrics", "equityCurve", "trades", "parameters"} <= _aliases(
        BacktestDetail
    )
    assert _aliases(PerformanceMetrics) == {
        "totalReturn", "cagr", "sharpe", "sortino", "maxDrawdown",
        "volatility", "winRate", "profitFactor", "totalTrades",
    }
    assert _aliases(EquityPoint) == {"date", "equity", "benchmark"}
    assert {"entryDate", "exitDate", "returnPct"} <= _aliases(Trade)


def test_run_submission_request_keys_are_camel_case() -> None:
    """The New Run form's payload. These names are final and already shipped."""
    assert _aliases(BacktestRunRequest) == {
        "name", "strategyKey", "startDate", "endDate", "initialCapital",
        "mode", "params",
    }


def test_backtest_detail_carries_progress_and_failure_reason() -> None:
    """Both additive: the client's Zod schema ignores keys it does not declare.

    ``progressPct`` drives the run progress bar; ``errorMessage`` is the only
    place a failed — or cancelled — run can say why it stopped.
    """
    assert {"progressPct", "errorMessage"} <= _aliases(BacktestDetail)
    # ...and neither leaks into the list row, whose shape is asserted exactly
    # above and is what every cached row in the client is parsed against.
    assert {"progressPct", "errorMessage"}.isdisjoint(_aliases(BacktestSummary))


def test_strategy_keys_are_camel_case() -> None:
    assert {
        "className", "runCount", "bestSharpe", "bestReturn", "lastRunAt",
    } <= _aliases(Strategy)


# ---------------------------------------------------------------------------
# Backtests — DB-backed since the repositories landed.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_backtest_list_is_paginated(
    client: TestClient, seeded_run: BacktestSummary
) -> None:
    response = client.get("/api/backtests", params={"page": 1, "pageSize": 2})
    assert response.status_code == 200

    body = response.json()
    assert set(body) == {"items", "total", "page", "pageSize"}
    assert body["page"] == 1
    assert body["pageSize"] == 2
    # The fixture's run is the newest, so page 1 is never empty here — which is
    # what makes the per-item key assertion below run at all.
    assert body["items"]
    assert len(body["items"]) <= 2
    assert body["total"] >= len(body["items"])

    for item in body["items"]:
        # camelCase is the contract, not a preference.
        assert set(item) == _aliases(BacktestSummary)


@pytest.mark.db
def test_backtest_list_filters_by_strategy(
    client: TestClient, seeded_run: BacktestSummary
) -> None:
    response = client.get(
        "/api/backtests", params={"strategyId": CONTRACT_RUN_STRATEGY}
    )
    assert response.status_code == 200

    items = response.json()["items"]
    # A filter that returns nothing would satisfy the "all match" assertion on
    # its own, so prove the matching row is actually there first.
    assert seeded_run.id in {item["id"] for item in items}
    assert all(item["strategyId"] == CONTRACT_RUN_STRATEGY for item in items)

    other = client.get("/api/backtests", params={"strategyId": "portfolio_1"})
    assert other.status_code == 200
    assert seeded_run.id not in {item["id"] for item in other.json()["items"]}


@pytest.mark.db
def test_backtest_list_rows_match_their_detail(
    client: TestClient, seeded_run: BacktestSummary
) -> None:
    """Whatever the list shows must be fetchable in full."""
    listing = client.get("/api/backtests", params={"pageSize": 3})
    assert listing.status_code == 200

    items = listing.json()["items"]
    assert items, "the seeded run must appear on the first page"

    for item in items:
        detail = client.get(f"/api/backtests/{item['id']}")
        assert detail.status_code == 200

        body = detail.json()
        assert body["id"] == item["id"]
        assert set(body["metrics"]) == _aliases(PerformanceMetrics)
        assert isinstance(body["equityCurve"], list)
        assert isinstance(body["trades"], list)


@pytest.mark.db
def test_unknown_backtest_is_404_with_a_message(client: TestClient) -> None:
    response = client.get("/api/backtests/does-not-exist")
    assert response.status_code == 404
    # The client's ApiError reads `detail`; without it the UI shows a blank error.
    assert response.json()["detail"]


@pytest.mark.db
def test_deleting_an_unknown_backtest_is_404(client: TestClient) -> None:
    # The 204 path needs a run to delete, which only the run pipeline creates;
    # tests/integration/test_repositories.py covers delete against a real row.
    assert client.delete("/api/backtests/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Strategies — DB-backed, seeded by scripts/seed_strategies.py.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_strategy_list_includes_run_aggregates(client: TestClient) -> None:
    response = client.get("/api/strategies")
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == len(body["items"])
    assert body["items"], "run scripts/seed_strategies.py to populate the registry"

    for strategy in body["items"]:
        assert set(strategy) == _aliases(Strategy)
        assert strategy["className"]
        assert strategy["parameters"]
        assert strategy["runCount"] >= 0


@pytest.mark.db
def test_strategy_list_hides_disabled_strategies(client: TestClient) -> None:
    keys = {item["id"] for item in client.get("/api/strategies").json()["items"]}
    # The seeded test harness is disabled; offering it to students would be a
    # regression, not a feature.
    assert "portfolio_dummy" not in keys


def test_strategy_submission_rejects_source_that_is_not_a_strategy(
    client: TestClient,
) -> None:
    """Submitting starts a validation *backtest*, so the source must be runnable.

    The upload endpoint no longer just files source away: it scans it, stores
    it, and queues a run against it. A file that defines no ``BasePortfolio``
    subclass has nothing to run, and is refused before anything is written —
    which is why this needs no database and no cleanup. The accepted path
    (draft status, hidden from the catalogue until it passes) is covered end to
    end in ``tests/integration/test_user_strategies.py``.
    """
    response = client.post(
        "/api/strategies",
        json={
            "name": "Contract test",
            "description": "",
            "source": "print(1)",
            "filename": None,
        },
    )
    assert response.status_code == 422

    detail = response.json()["detail"]
    # A string, not FastAPI's list of error objects: the client shows it as-is.
    assert isinstance(detail, str)
    assert "BasePortfolio" in detail


# ---------------------------------------------------------------------------
# POST /strategies/check: the pre-flight the editor calls before submitting.
# Static, so no database, no store and no worker: every case here runs offline.
# ---------------------------------------------------------------------------

# A strategy written the way the engine expects one. Kept whole rather than
# assembled per test so "compatible" is asserted against something a student
# could actually paste in.
COMPATIBLE_SOURCE = """
from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class ContractCheckStrategy(BasePortfolio):
    def OnData(self, context: StrategyContext):
        for ticker in self.tickers:
            context.Order.SetHoldings(ticker, 1 / len(self.tickers))
"""


def _check(client: TestClient, source: str) -> dict:
    """POST the source and assert the endpoint itself succeeded.

    Every caller asserts 200 first: a verdict of "incompatible" is a successful
    check, and a non-200 means the endpoint broke rather than the source.
    """
    response = client.post("/api/strategies/check", json={"source": source})
    assert response.status_code == 200, response.text
    return response.json()


def test_check_accepts_a_strategy_written_against_the_engine(
    client: TestClient,
) -> None:
    body = _check(client, COMPATIBLE_SOURCE)
    assert body["status"] == "compatible"
    assert body["ok"] is True
    assert body["className"] == "ContractCheckStrategy"
    assert body["issues"] == []
    assert body["message"]


def test_check_keys_are_camel_case() -> None:
    assert _aliases(StrategyCheckResult) == {
        "status",
        "ok",
        "className",
        "issues",
        "warnings",
        "message",
    }
    assert _aliases(CompatibilityIssue) == {"line", "message"}


def test_incompatible_source_still_answers_200(client: TestClient) -> None:
    """The contract the client depends on: a verdict, not an HTTP error.

    The editor lists every problem at once. A 4xx would flatten them into one
    ``detail`` string and would also say the *request* was wrong, which it was
    not.
    """
    body = _check(client, "print(1)")
    assert body["status"] == "incompatible"
    assert body["ok"] is False
    assert body["className"] is None
    assert "BasePortfolio" in body["issues"][0]["message"]


def test_check_reports_every_problem_with_its_line(client: TestClient) -> None:
    source = "import os\n" + COMPATIBLE_SOURCE
    body = _check(client, source)

    assert body["ok"] is False
    # The banned import *and* the strategy class: finding one does not stop the
    # read, and the class name is still reported so the editor can name it.
    assert body["className"] == "ContractCheckStrategy"
    assert len(body["issues"]) == 1
    assert body["issues"][0]["line"] == 1
    assert "os" in body["issues"][0]["message"]


def test_check_catches_the_lowercase_ondata(client: TestClient) -> None:
    """The single most likely reason a correct-looking strategy does not run."""
    source = COMPATIBLE_SOURCE.replace("def OnData", "def on_data")
    body = _check(client, source)

    assert body["ok"] is False
    assert "OnData" in body["issues"][0]["message"]


def test_check_reports_a_missing_super_init_as_a_warning(client: TestClient) -> None:
    """Warnings inform; they never make the answer "no"."""
    source = COMPATIBLE_SOURCE.replace(
        "    def OnData",
        "    def __init__(self, db_connector, executor, debug=False,\n"
        "                 config_dict=None, backtest_start_date=None,\n"
        "                 order_manager=None):\n"
        "        self.tickers = []\n"
        "\n"
        "    def OnData",
    )
    body = _check(client, source)

    assert body["ok"] is True
    assert body["issues"] == []
    assert "super().__init__" in body["warnings"][0]["message"]


# Each of the four below is a case that shipped wrong and was caught by running
# the same source past the real engine loader. They assert agreement with what
# the loader actually does, not with what the check reads nicely as.


def test_check_follows_inheritance_through_the_file(client: TestClient) -> None:
    """A shared base counts as a strategy, because the loader counts it.

    Matching on ``class X(BasePortfolio)`` alone made a factored-out base look
    like one strategy that forgot ``OnData``: the wrong class named and the
    wrong fix suggested, for a file the loader refuses as ambiguous.
    """
    source = COMPATIBLE_SOURCE.replace(
        "class ContractCheckStrategy(BasePortfolio):",
        "class SharedBase(BasePortfolio):\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class ContractCheckStrategy(SharedBase):",
    )
    body = _check(client, source)

    assert body["ok"] is False
    message = body["issues"][0]["message"]
    assert "2 strategies" in message
    assert "SharedBase" in message and "ContractCheckStrategy" in message


def test_check_accepts_ondata_bound_by_assignment(client: TestClient) -> None:
    """``OnData = some_function`` is a method the engine runs, so it passes.

    Nothing about the target is readable from the assignment, and rejecting it
    would refuse code that demonstrably works.
    """
    source = COMPATIBLE_SOURCE.replace(
        "    def OnData(self, context: StrategyContext):\n"
        "        for ticker in self.tickers:\n"
        "            context.Order.SetHoldings(ticker, 1 / len(self.tickers))\n",
        "    OnData = _on_data\n",
    ).replace(
        "class ContractCheckStrategy(BasePortfolio):",
        "def _on_data(self, context):\n"
        "    for ticker in self.tickers:\n"
        "        context.buy(ticker)\n"
        "\n"
        "\n"
        "class ContractCheckStrategy(BasePortfolio):",
    )
    body = _check(client, source)

    assert body["ok"] is True, body["issues"]


def test_check_accepts_a_staticmethod_ondata(client: TestClient) -> None:
    """``self.OnData(context)`` on a staticmethod passes one argument, not two."""
    source = COMPATIBLE_SOURCE.replace(
        "    def OnData(self, context: StrategyContext):",
        "    @staticmethod\n    def OnData(context):",
    ).replace("self.tickers", "context.Portfolio.positions")
    body = _check(client, source)

    assert body["ok"] is True, body["issues"]


def test_a_staticmethod_ondata_still_needs_its_context(client: TestClient) -> None:
    """The staticmethod allowance is one argument fewer, not no check at all."""
    source = COMPATIBLE_SOURCE.replace(
        "    def OnData(self, context: StrategyContext):\n"
        "        for ticker in self.tickers:\n"
        "            context.Order.SetHoldings(ticker, 1 / len(self.tickers))\n",
        "    @staticmethod\n    def OnData():\n        pass\n",
    )
    body = _check(client, source)

    assert body["ok"] is False
    assert "OnData(context)" in body["issues"][0]["message"]


def test_check_reports_invalid_python_without_crashing(client: TestClient) -> None:
    body = _check(client, "class Broken(BasePortfolio)\n    pass\n")
    assert body["ok"] is False
    assert "not valid Python" in body["issues"][0]["message"]


def test_check_agrees_with_what_submission_accepts(client: TestClient) -> None:
    """The check must not pass source the upload endpoint then refuses.

    The two share one scan; this pins that they stay shared. Asserted on a
    rejection because it needs no database; the accepting path is covered end
    to end in ``tests/integration/test_user_strategies.py``.
    """
    source = "import socket\n" + COMPATIBLE_SOURCE
    assert _check(client, source)["ok"] is False

    submission = client.post(
        "/api/strategies",
        json={"name": "Check agreement", "description": "", "source": source,
              "filename": None},
    )
    assert submission.status_code == 422


def test_oversized_source_is_rejected_by_the_check_too(client: TestClient) -> None:
    """The one case that is a real HTTP error: the request is too big to read."""
    response = client.post(
        "/api/strategies/check", json={"source": "x" * (256 * 1024 + 1)}
    )
    assert response.status_code == 413


def test_check_needs_a_source(client: TestClient) -> None:
    response = client.post("/api/strategies/check", json={"source": ""})
    assert response.status_code == 422


def test_oversized_strategy_source_is_rejected(client: TestClient) -> None:
    # Rejected on size before any database work, so this needs no marker.
    response = client.post(
        "/api/strategies",
        json={"name": "Big", "description": "", "source": "x" * (256 * 1024 + 1),
              "filename": None},
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# /live/* — still sample data, deliberately. No database involved.
# ---------------------------------------------------------------------------


def test_portfolio_list_and_detail(client: TestClient) -> None:
    listing = client.get("/api/live/portfolios")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["strategyClass"]

    detail = client.get("/api/live/portfolios/portfolio_1")
    assert detail.status_code == 200

    body = detail.json()
    assert body["positions"]
    # The config block keeps MQSMaster's SCREAMING_SNAKE keys verbatim.
    assert body["config"]["PORTFOLIO_ID"] == "portfolio_1"
    assert body["config"]["TICKERS"]


def test_equity_and_composition_series_line_up(client: TestClient) -> None:
    equity = client.get("/api/live/portfolios/portfolio_1/equity", params={"days": 30})
    assert equity.status_code == 200
    assert len(equity.json()["points"]) == 30

    composition = client.get(
        "/api/live/portfolios/portfolio_1/composition", params={"days": 12}
    )
    assert composition.status_code == 200

    body = composition.json()
    # The client's Zod refine rejects the payload unless every series matches
    # the timestamp count.
    assert len(body["cash"]) == len(body["timestamps"]) == 12
    assert all(len(series) == 12 for series in body["holdings"].values())


def test_executions_paginate_and_filter(client: TestClient) -> None:
    response = client.get(
        "/api/live/portfolios/portfolio_1/executions",
        params={"page": 2, "pageSize": 5, "ticker": "AAPL"},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["page"] == 2
    assert all(item["ticker"] == "AAPL" for item in body["items"])


def test_correlation_matrix_is_square_and_symmetric(client: TestClient) -> None:
    response = client.get("/api/live/portfolios/portfolio_1/correlations")
    assert response.status_code == 200

    body = response.json()
    size = len(body["tickers"])
    matrix = body["matrix"]

    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)
    assert all(matrix[i][i] == 1.0 for i in range(size))
    assert all(matrix[i][j] == matrix[j][i] for i in range(size) for j in range(size))


def test_unknown_portfolio_is_404_on_every_subresource(client: TestClient) -> None:
    for suffix in ("", "/equity", "/composition", "/executions", "/correlations"):
        response = client.get(f"/api/live/portfolios/nope{suffix}")
        assert response.status_code == 404, suffix


def test_system_status_headline_matches_worst_service(client: TestClient) -> None:
    response = client.get("/api/live/system/status")
    assert response.status_code == 200

    body = response.json()
    states = {service["state"] for service in body["services"]}
    if "down" in states:
        assert body["state"] == "down"
    elif "degraded" in states:
        assert body["state"] == "degraded"
    else:
        assert body["state"] == "up"


def test_log_tail_respects_size(client: TestClient) -> None:
    response = client.get("/api/live/system/logs", params={"size": 7})
    assert response.status_code == 200

    body = response.json()
    assert len(body["entries"]) == 7
    assert body["entries"][0]["level"] in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }
