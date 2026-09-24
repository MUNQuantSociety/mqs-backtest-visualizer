"""The run form's sentiment gate: submit-time validation and worker hand-off."""

import math
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.services import run_controls
from src.services.run_controls import split_controls
from src.workers import run_job as worker
from src.workers.sentiment_gate_loader import SentimentGateUnavailable

ENABLED = {"enabled": True, "threshold": -0.25}


@pytest.fixture
def news_configured(monkeypatch):
    monkeypatch.setattr(run_controls, "settings", SimpleNamespace(news_database_configured=True))


def test_enabled_gate_is_kept_as_a_control(news_configured):
    _, controls, _ = split_controls({"sentimentGate": ENABLED}, ["AAPL"], "event")

    assert controls["sentimentGate"] == {"enabled": True, "threshold": -0.25}


def test_disabled_gate_stores_nothing():
    _, controls, _ = split_controls(
        {"sentimentGate": {"enabled": False, "threshold": -0.25}}, ["AAPL"], "event"
    )

    assert "sentimentGate" not in controls


@pytest.mark.parametrize("threshold", [0.05, -1.05, math.nan, True, "-0.2", None])
def test_gate_rejects_a_threshold_outside_minus_one_to_zero(news_configured, threshold):
    with pytest.raises(ValueError, match="threshold"):
        split_controls(
            {"sentimentGate": {"enabled": True, "threshold": threshold}}, ["AAPL"], "event"
        )


@pytest.mark.parametrize("threshold", [-1, 0])
def test_gate_accepts_the_threshold_bounds(news_configured, threshold):
    _, controls, _ = split_controls(
        {"sentimentGate": {"enabled": True, "threshold": threshold}}, ["AAPL"], "event"
    )

    assert controls["sentimentGate"]["threshold"] == float(threshold)


@pytest.mark.parametrize("raw", [True, {"threshold": -0.2}, {"enabled": "yes"}])
def test_gate_rejects_a_malformed_control(raw):
    with pytest.raises(ValueError, match="enabled"):
        split_controls({"sentimentGate": raw}, ["AAPL"], "event")


def test_gate_requires_event_mode(news_configured):
    with pytest.raises(ValueError, match="event mode"):
        split_controls({"sentimentGate": ENABLED}, ["AAPL"], "fast")


def test_gate_requires_the_live_news_database(monkeypatch):
    monkeypatch.setattr(run_controls, "settings", SimpleNamespace(news_database_configured=False))

    with pytest.raises(ValueError, match="news database"):
        split_controls({"sentimentGate": ENABLED}, ["AAPL"], "event")


def _context(params):
    return worker._RunContext(
        run_id=uuid4(),
        strategy_key="sample",
        class_path="sample:Strategy",
        start_date=date(2026, 3, 2),
        end_date=date(2026, 7, 15),
        initial_capital=100_000,
        mode="event",
        params=params,
    )


HEARTBEAT = SimpleNamespace(on_progress=lambda *args: None, should_cancel=lambda: False)


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "settings", SimpleNamespace(artifact_dir=tmp_path))


def test_worker_loads_the_gate_for_the_run_universe_and_window(artifacts, monkeypatch):
    calls = {}

    def fake_loader(**kwargs):
        calls.update(kwargs)
        return "GATE"

    monkeypatch.setattr(worker, "load_sentiment_gate", fake_loader)
    context = _context({"universe": ["AAPL", "MSFT"], "sentimentGate": {"enabled": True, "threshold": -0.3}})

    request = worker._build_request(context, HEARTBEAT)

    assert request.sentiment_gate == "GATE"
    assert calls == {
        "threshold": -0.3,
        "tickers": ["AAPL", "MSFT"],
        "start_date": date(2026, 3, 2),
        "end_date": date(2026, 7, 15),
    }


def test_worker_keeps_the_gate_control_out_of_strategy_params(artifacts, monkeypatch):
    monkeypatch.setattr(worker, "load_sentiment_gate", lambda **_kwargs: "GATE")
    context = _context({"universe": ["AAPL"], "sentimentGate": {"enabled": True, "threshold": -0.3}})

    request = worker._build_request(context, HEARTBEAT)

    assert "sentimentGate" not in request.params


def test_worker_fails_the_request_when_the_gate_cannot_load(artifacts, monkeypatch):
    def broken_loader(**_kwargs):
        raise SentimentGateUnavailable("unreachable")

    monkeypatch.setattr(worker, "load_sentiment_gate", broken_loader)
    context = _context({"universe": ["AAPL"], "sentimentGate": {"enabled": True, "threshold": -0.3}})

    with pytest.raises(SentimentGateUnavailable):
        worker._build_request(context, HEARTBEAT)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeConnection:
    def __init__(self, rows_by_ticker, log):
        self._rows = rows_by_ticker
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _statement, params):
        self._log.append(("execute", params["ticker"]))
        return _Rows(self._rows.get(params["ticker"], []))

    def commit(self):
        self._log.append(("commit",))

    def rollback(self):
        self._log.append(("rollback",))


class _FakeEngine:
    def __init__(self, rows_by_ticker):
        self.log = []
        self._rows = rows_by_ticker

    def connect(self):
        return _FakeConnection(self._rows, self.log)

    def dispose(self):
        self.log.append(("dispose",))


def _load(engine, tickers=("aapl", "MSFT")):
    from src.workers.sentiment_gate_loader import load_sentiment_gate

    return load_sentiment_gate(
        threshold=-0.25,
        tickers=list(tickers),
        start_date=date(2026, 3, 2),
        end_date=date(2026, 7, 15),
        engine_factory=lambda: engine,
    )


def test_loader_builds_a_gate_from_each_ticker_s_scores():
    from datetime import datetime

    engine = _FakeEngine({"AAPL": [(datetime(2026, 3, 1, 15), -0.5)]})

    gate = _load(engine)

    assert gate.coverage()["AAPL"]["articleCount"] == 1
    assert gate.coverage()["MSFT"]["articleCount"] == 0


def test_loader_rolls_back_and_never_commits():
    engine = _FakeEngine({})

    _load(engine)

    assert ("commit",) not in engine.log
    assert engine.log[-2:] == [("rollback",), ("dispose",)]


def test_loader_reports_an_unconfigured_database_as_unavailable():
    from src.db.engine import NewsDatabaseNotConfigured
    from src.workers.sentiment_gate_loader import load_sentiment_gate

    def unconfigured():
        raise NewsDatabaseNotConfigured("set NEWS_POSTGRES_*")

    with pytest.raises(SentimentGateUnavailable, match="NEWS_POSTGRES"):
        load_sentiment_gate(-0.25, ["AAPL"], date(2026, 3, 2), date(2026, 7, 15), unconfigured)


def test_loader_reports_a_failed_read_as_unavailable():
    class BrokenEngine(_FakeEngine):
        def connect(self):
            raise OSError("connection refused")

    with pytest.raises(SentimentGateUnavailable, match="OSError"):
        _load(BrokenEngine({}))


def test_run_single_refuses_the_gate_in_fast_mode(tmp_path):
    from engine.contracts import RunRequest
    from engine.core.sentiment_gate import SentimentGate
    from engine.run_single import run_single

    result = run_single(
        RunRequest(
            run_id=str(uuid4()),
            strategy_key="portfolio_dummy",
            class_path="engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
            start_date="2026-06-24",
            end_date="2026-07-15",
            initial_capital=100_000.0,
            mode="fast",
            artifact_dir=str(tmp_path),
            sentiment_gate=SentimentGate(threshold=-0.25, articles={}),
        )
    )

    assert result.status == "failed"
    assert "sentiment gate" in (result.error or "")


def test_worker_runs_ungated_without_a_gate_control(artifacts, monkeypatch):
    monkeypatch.setattr(
        worker, "load_sentiment_gate", lambda **_kwargs: pytest.fail("gate loaded for nothing")
    )

    request = worker._build_request(_context({"universe": ["AAPL"]}), HEARTBEAT)

    assert request.sentiment_gate is None
