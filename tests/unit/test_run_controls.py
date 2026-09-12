"""Regression: the real browser form's reserved params must reach execution."""

import math
from datetime import date
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
        },
    )
    heartbeat = SimpleNamespace(
        on_progress=lambda *args: None, should_cancel=lambda: False
    )
    request = worker._build_request(context, heartbeat)
    assert request.params == {"LOOKBACK_DAYS": 30}
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
    dispatch.assert_awaited_once()
