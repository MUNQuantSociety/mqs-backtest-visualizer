"""Regression: the real browser form's reserved params must reach execution."""

import math
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.services.run_controls import split_controls


def test_real_form_payload_keeps_strategy_params_separate():
    params, controls, universe = split_controls(
        {
            "universe": ["AAPL", "MSFT"],
            "slippageBps": 5,
            "commissionPerShare": 0.005,
            "signals": [],
            "sentimentGate": {"enabled": False, "threshold": -0.25},
            "LOOKBACK_DAYS": 30,
        },
        ["AAPL", "MSFT"],
        "event",
    )
    assert params == {"LOOKBACK_DAYS": 30}
    assert controls == {
        "universe": universe,
        "slippageBps": 5.0,
        "commissionPerShare": 0.005,
    }
    assert "WEIGHTS" not in controls  # Preserve existing strategy allocations.


def test_explicit_universe_override_is_equal_weighted():
    _, controls, universe = split_controls(
        {"universe": ["msft", " NVDA "]}, ["AAPL"], "event"
    )
    assert universe == ["MSFT", "NVDA"]
    assert controls["TICKERS"] == universe
    assert controls["WEIGHTS"] == {"MSFT": 0.5, "NVDA": 0.5}


def test_omitted_controls_preserve_legacy_defaults():
    assert split_controls({}, ["AAPL"], "event") == ({}, {}, ["AAPL"])


@pytest.mark.parametrize(
    "raw",
    [
        {"universe": []},
        {"universe": "AAPL"},
        {"universe": [True]},
        {"universe": ["AAPL", "aapl"]},
        {"universe": ["../../secret"]},
        {"slippageBps": -1},
        {"slippageBps": math.nan},
        {"slippageBps": True},
        {"commissionPerShare": math.inf},
        {"commissionPerShare": "0.005"},
        {"signals": ["RSI 14"]},
        {"sentimentGate": {"enabled": True}},
    ],
)
def test_invalid_or_unsupported_controls_fail_explicitly(raw):
    with pytest.raises(ValueError):
        split_controls(raw, ["AAPL"], "event")


def test_custom_weights_become_the_run_allocation():
    _, controls, universe = split_controls(
        {"universe": ["AAPL", "MSFT"], "weights": {"aapl": 0.6, "MSFT": 0.3}},
        ["AAPL", "MSFT"],
        "event",
    )
    assert controls["weights"] == {"AAPL": 0.6, "MSFT": 0.3}
    assert controls["TICKERS"] == universe
    assert controls["WEIGHTS"] == {"AAPL": 0.6, "MSFT": 0.3}


def test_custom_weights_apply_to_the_strategy_universe_when_it_is_unchanged():
    _, controls, _ = split_controls({"weights": {"AAPL": 1.0}}, ["AAPL"], "event")
    assert controls["TICKERS"] == ["AAPL"]
    assert controls["WEIGHTS"] == {"AAPL": 1.0}


def test_custom_weights_override_the_equal_split_of_a_changed_universe():
    _, controls, _ = split_controls(
        {"universe": ["MSFT", "NVDA"], "weights": {"MSFT": 0.8, "NVDA": 0.2}},
        ["AAPL"],
        "event",
    )
    assert controls["WEIGHTS"] == {"MSFT": 0.8, "NVDA": 0.2}


def test_weights_may_leave_part_of_the_book_in_cash():
    _, controls, _ = split_controls(
        {"weights": {"AAPL": 0.5, "MSFT": 0.0}}, ["AAPL", "MSFT"], "event"
    )
    assert controls["WEIGHTS"] == {"AAPL": 0.5, "MSFT": 0.0}


@pytest.mark.parametrize(
    "weights",
    [
        {"AAPL": 0.7, "MSFT": 0.4},  # over 100%: leverage
        {"AAPL": -0.1, "MSFT": 0.5},
        {"AAPL": math.nan, "MSFT": 0.5},
        {"AAPL": math.inf, "MSFT": 0.0},
        {"AAPL": True, "MSFT": 0.5},
        {"AAPL": "0.5", "MSFT": 0.5},
        {"AAPL": 0.0, "MSFT": 0.0},  # nothing allocated
        {"AAPL": 1.0},  # a universe ticker missing
        {"AAPL": 0.5, "MSFT": 0.3, "NVDA": 0.2},  # not in the universe
        {"AAPL": 0.5, "aapl": 0.3, "MSFT": 0.2},  # same ticker twice
        ["AAPL", "MSFT"],
        "AAPL=1",
    ],
)
def test_invalid_weights_fail_explicitly(weights):
    with pytest.raises(ValueError, match="weights"):
        split_controls({"weights": weights}, ["AAPL", "MSFT"], "event")


def test_weights_summing_to_one_within_rounding_are_accepted():
    _, controls, _ = split_controls(
        {"weights": {"AAPL": 0.1, "MSFT": 0.2, "NVDA": 0.7000000001}},
        ["AAPL", "MSFT", "NVDA"],
        "event",
    )
    assert sum(controls["WEIGHTS"].values()) == pytest.approx(1.0)


def test_fast_rejects_per_share_commission():
    with pytest.raises(ValueError, match="event mode"):
        split_controls({"commissionPerShare": 0.005}, ["AAPL"], "fast")


def test_worker_converts_bps_and_keeps_control_keys_out_of_strategy(
    tmp_path, monkeypatch
):
    from src.workers import run_job as worker

    monkeypatch.setattr(worker, "settings", SimpleNamespace(artifact_dir=tmp_path))
    context = worker._RunContext(
        run_id=uuid4(),
        strategy_key="sample",
        class_path="sample:Strategy",
        start_date=date(2026, 3, 2),
        end_date=date(2026, 7, 15),
        initial_capital=100_000,
        mode="event",
        params={
            "LOOKBACK_DAYS": 30,
            "universe": ["AAPL"],
            "slippageBps": 5,
            "commissionPerShare": 0.005,
            "weights": {"AAPL": 1.0},
            "WEIGHTS": {"AAPL": 1.0},
        },
    )
    heartbeat = SimpleNamespace(
        on_progress=lambda *args: None, should_cancel=lambda: False
    )
    request = worker._build_request(context, heartbeat)
    # The recorded choice is not a strategy parameter; the engine reads WEIGHTS.
    assert request.params == {"LOOKBACK_DAYS": 30, "WEIGHTS": {"AAPL": 1.0}}
    assert request.slippage == pytest.approx(0.0005)
    assert request.commission_per_share == pytest.approx(0.005)
    assert "slippageBps" in context.params  # Persisted provenance is unchanged.


def test_submission_validates_selected_universe_and_dispatches(monkeypatch):
    import asyncio
    from src.schemas.backtests import BacktestRunRequest
    from src.services import backtests

    strategy = backtests._RunnableStrategy("sample", ["AAPL"], [])
    monkeypatch.setattr(
        backtests, "_load_runnable_strategy", AsyncMock(return_value=strategy)
    )
    coverage = AsyncMock()
    create = AsyncMock(return_value=SimpleNamespace(id="run"))
    dispatch = AsyncMock(return_value="queued")
    monkeypatch.setattr(backtests, "_validated_coverage", coverage)
    monkeypatch.setattr(backtests, "create_backtest_run", create)
    monkeypatch.setattr(backtests, "_dispatch", dispatch)
    request = BacktestRunRequest(
        name="browser",
        strategy_key="sample",
        start_date="2026-03-02",
        end_date="2026-07-15",
        initial_capital=100_000,
        params={"universe": ["MSFT"], "slippageBps": 5, "commissionPerShare": 0.005},
    )
    assert asyncio.run(backtests.submit_backtest_run(request)) == "queued"
    assert coverage.await_args.args[0] == ["MSFT"]
    assert create.await_args.kwargs["symbol"] == "MSFT"
    assert create.await_args.kwargs["params"]["TICKERS"] == ["MSFT"]
    assert create.await_args.kwargs["params"]["mode"] == "event"
    dispatch.assert_awaited_once()


@pytest.mark.parametrize("mode", ["fast", " FAST "])
@pytest.mark.parametrize("commission", [0, 0.005])
def test_new_fast_submission_rejected_before_coverage_or_dispatch(
    monkeypatch, mode, commission
):
    import asyncio
    from src.schemas.backtests import BacktestRunRequest
    from src.services import backtests

    monkeypatch.setattr(
        backtests,
        "_load_runnable_strategy",
        AsyncMock(return_value=backtests._RunnableStrategy("sample", ["AAPL"], [])),
    )
    coverage = AsyncMock()
    create = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(backtests, "_validated_coverage", coverage)
    monkeypatch.setattr(backtests, "create_backtest_run", create)
    monkeypatch.setattr(backtests, "_dispatch", dispatch)
    request = BacktestRunRequest(
        name="browser",
        strategy_key="sample",
        start_date="2026-03-02",
        end_date="2026-07-15",
        initial_capital=100_000,
        mode=mode,
        params={"commissionPerShare": commission},
    )

    with pytest.raises(backtests.RunSubmissionError, match="mode must be 'event'"):
        asyncio.run(backtests.submit_backtest_run(request))

    coverage.assert_not_called()
    create.assert_not_called()
    dispatch.assert_not_called()


def test_existing_fast_report_remains_readable_and_exportable():
    import json
    from src.repositories import reports
    from src.services import backtests
    from src.services.report_exports import export_report

    stored = SimpleNamespace(
        id=uuid4(),
        name="Historical fast run",
        strategy_key="sample",
        created_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
        version=reports.REPORT_VERSION,
        results={
            "strategyName": "Sample",
            "symbol": "AAPL",
            "timeframe": "1d",
            "startDate": "2026-03-02",
            "endDate": "2026-03-04",
            "initialCapital": 100,
            "finalEquity": 100,
            "totalReturn": 0,
            "sharpe": 0,
            "maxDrawdown": 0,
            "metrics": backtests._to_metrics(None).model_dump(by_alias=True),
            "equityCurve": [{"date": "2026-03-04", "equity": 100}],
            "trades": [],
            "parameters": {"mode": "fast", "universe": ["AAPL"]},
        },
    )

    detail = reports.to_detail(stored)
    assert detail.status == "completed"
    assert detail.parameters == stored.results["parameters"]
    exported = json.loads(export_report(detail, "report.json").content)
    assert exported["parameters"] == stored.results["parameters"]
    assert exported["equityCurve"] == [
        {"date": "2026-03-04", "equity": 100.0, "benchmark": None}
    ]


def test_daily_bar_interval_is_recorded_without_an_engine_overlay():
    _, controls, _ = split_controls({"barIntervalSeconds": 86400}, ["AAPL"], "event")
    assert controls == {"barIntervalSeconds": 86400}


def test_intraday_bar_interval_sets_bar_size_and_a_decision_on_every_bar():
    params, controls, _ = split_controls({"barIntervalSeconds": 3600}, ["AAPL"], "event")
    assert params == {}
    assert controls == {
        "barIntervalSeconds": 3600,
        "BAR_INTERVAL_SECONDS": 3600,
        "INTERVAL": 0,
    }


@pytest.mark.parametrize("value", [0, 59, 90, 86401, True, "300", None, math.nan])
def test_unsupported_bar_interval_is_refused(value):
    with pytest.raises(ValueError, match="Bar interval must be one of"):
        split_controls({"barIntervalSeconds": value}, ["AAPL"], "event")


def test_fast_mode_refuses_intraday_bar_interval():
    with pytest.raises(ValueError, match="Intraday bars require event mode"):
        split_controls({"barIntervalSeconds": 60}, ["AAPL"], "fast")


def test_fast_mode_accepts_a_daily_bar_interval():
    _, controls, _ = split_controls({"barIntervalSeconds": 86400}, ["AAPL"], "fast")
    assert controls == {"barIntervalSeconds": 86400}


def test_worker_keeps_bar_interval_label_out_of_the_engine_overlay(tmp_path, monkeypatch):
    from src.workers import run_job as worker

    monkeypatch.setattr(worker, "settings", SimpleNamespace(artifact_dir=tmp_path))
    context = worker._RunContext(
        run_id=uuid4(),
        strategy_key="sample",
        class_path="sample:Strategy",
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 6),
        initial_capital=100_000,
        mode="event",
        params={"barIntervalSeconds": 300, "BAR_INTERVAL_SECONDS": 300, "INTERVAL": 0},
    )
    heartbeat = SimpleNamespace(on_progress=lambda *args: None, should_cancel=lambda: False)
    request = worker._build_request(context, heartbeat)
    assert request.params == {"BAR_INTERVAL_SECONDS": 300, "INTERVAL": 0}


def test_oversized_intraday_submission_is_refused_before_queueing(monkeypatch):
    import asyncio
    from src.schemas.backtests import BacktestRunRequest
    from src.services import backtests

    strategy = backtests._RunnableStrategy("sample", ["AAPL"], [])
    monkeypatch.setattr(backtests, "_load_runnable_strategy", AsyncMock(return_value=strategy))
    create = AsyncMock()
    monkeypatch.setattr(backtests, "create_backtest_run", create)
    request = BacktestRunRequest(
        name="too big",
        strategy_key="sample",
        # 10 tickers x ~270 weekdays x 390 one-minute bars is past the limit.
        start_date="2025-07-01",
        end_date="2026-07-15",
        initial_capital=100_000,
        params={"universe": [f"T{i}" for i in range(10)], "barIntervalSeconds": 60},
    )
    with pytest.raises(backtests.RunSubmissionError, match="would load about"):
        asyncio.run(backtests.submit_backtest_run(request))
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("params", "label"),
    [
        ({}, "1d"),
        ({"barIntervalSeconds": 86400}, "1d"),
        ({"barIntervalSeconds": 60}, "1m"),
        ({"barIntervalSeconds": 3600}, "1h"),
        ({"barIntervalSeconds": True}, "1d"),
        ({"barIntervalSeconds": [60]}, "1d"),
    ],
)
def test_timeframe_label_names_the_run_bar_size(params, label):
    from src.services.run_controls import timeframe_label

    assert timeframe_label(params) == label
