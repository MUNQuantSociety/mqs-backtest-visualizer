"""Contract tests for the endpoints the frontend calls.

The client parses every response with Zod, so a renamed or snake_case key is a
hard failure in the browser, not a cosmetic difference. These tests assert the
shape the frontend expects — they are the cheap early warning for that.

    pytest tests/unit/test_api_contract.py -v
"""

from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_backtest_list_is_paginated() -> None:
    response = client.get("/api/backtests", params={"page": 1, "pageSize": 2})
    assert response.status_code == 200

    body = response.json()
    assert set(body) == {"items", "total", "page", "pageSize"}
    assert len(body["items"]) == 2
    assert body["total"] > 2

    # camelCase is the contract, not a preference.
    assert "strategyId" in body["items"][0]
    assert "maxDrawdown" in body["items"][0]


def test_backtest_list_filters_by_strategy() -> None:
    response = client.get("/api/backtests", params={"strategyId": "portfolio_1"})
    assert response.status_code == 200

    items = response.json()["items"]
    assert items
    assert all(item["strategyId"] == "portfolio_1" for item in items)


def test_backtest_detail_carries_curve_and_trades() -> None:
    response = client.get("/api/backtests/bt-001")
    assert response.status_code == 200

    body = response.json()
    assert body["equityCurve"]
    assert body["trades"]
    assert set(body["metrics"]) == {
        "totalReturn", "cagr", "sharpe", "sortino", "maxDrawdown",
        "volatility", "winRate", "profitFactor", "totalTrades",
    }


def test_unknown_backtest_is_404_with_a_message() -> None:
    response = client.get("/api/backtests/does-not-exist")
    assert response.status_code == 404
    # The client's ApiError reads `detail`; without it the UI shows a blank error.
    assert response.json()["detail"]


def test_delete_backtest_returns_no_content() -> None:
    assert client.delete("/api/backtests/bt-001").status_code == 204
    assert client.delete("/api/backtests/does-not-exist").status_code == 404


def test_strategy_list_includes_run_aggregates() -> None:
    response = client.get("/api/strategies")
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == len(body["items"])

    strategy = body["items"][0]
    assert strategy["className"]
    assert strategy["parameters"]
    assert "runCount" in strategy and "bestSharpe" in strategy


def test_strategy_submission_is_stored_as_a_draft() -> None:
    response = client.post(
        "/api/strategies",
        json={"name": "Test", "description": "", "source": "print(1)", "filename": None},
    )
    assert response.status_code == 201

    body = response.json()
    # Untrusted code must never come back as validated or active.
    assert body["status"] == "draft"


def test_oversized_strategy_source_is_rejected() -> None:
    response = client.post(
        "/api/strategies",
        json={"name": "Big", "description": "", "source": "x" * (256 * 1024 + 1),
              "filename": None},
    )
    assert response.status_code == 413


def test_portfolio_list_and_detail() -> None:
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


def test_equity_and_composition_series_line_up() -> None:
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


def test_executions_paginate_and_filter() -> None:
    response = client.get(
        "/api/live/portfolios/portfolio_1/executions",
        params={"page": 2, "pageSize": 5, "ticker": "AAPL"},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["page"] == 2
    assert all(item["ticker"] == "AAPL" for item in body["items"])


def test_correlation_matrix_is_square_and_symmetric() -> None:
    response = client.get("/api/live/portfolios/portfolio_1/correlations")
    assert response.status_code == 200

    body = response.json()
    size = len(body["tickers"])
    matrix = body["matrix"]

    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)
    assert all(matrix[i][i] == 1.0 for i in range(size))
    assert all(matrix[i][j] == matrix[j][i] for i in range(size) for j in range(size))


def test_unknown_portfolio_is_404_on_every_subresource() -> None:
    for suffix in ("", "/equity", "/composition", "/executions", "/correlations"):
        response = client.get(f"/api/live/portfolios/nope{suffix}")
        assert response.status_code == 404, suffix


def test_system_status_headline_matches_worst_service() -> None:
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


def test_log_tail_respects_size() -> None:
    response = client.get("/api/live/system/logs", params={"size": 7})
    assert response.status_code == 200

    body = response.json()
    assert len(body["entries"]) == 7
    assert body["entries"][0]["level"] in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }
