"""``run_single`` against the live database, on the smallest window that works.

Marked ``db`` because these are the only tests that prove the whole seam —
adapter, market data, indicator warm-up, event loop, reporting, metrics — is
actually wired together. They are slow (minutes: the database is remote and
each strategy warms three indicators per ticker before the first bar), so the
successful run is executed exactly once and shared by the assertions about it.

The window is pinned to dates that exist in ``public.market_data`` rather than
computed from ``today``: coverage is backfilled in batches and ends well before
the current date, so a relative window would make a healthy engine look broken.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from engine.contracts import METRIC_KEYS, RunRequest, RunResult
from engine.run_single import run_single

pytestmark = pytest.mark.db

DUMMY_CLASS_PATH = (
    "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy"
)

# A three-week window inside verified coverage (market_data holds AAPL from
# 2019-11-11 to 2026-07-15). Short on purpose: this proves the pipeline runs,
# not that the strategy is any good.
WINDOW_START = "2026-06-24"
WINDOW_END = "2026-07-15"

# The overlay that makes these tests affordable. Constructing the strategy
# warms three indicators *per ticker* straight from the database, and each of
# those queries pulls months of intraday rows across the network, so cutting
# the universe to one ticker cuts the wall clock roughly fourfold. A shorter
# data lookback trims the fetch the same way. Every stage still runs.
FAST_PARAMS = {
    "LOOKBACK_DAYS": 5,
    "TICKERS": ["AAPL"],
    "WEIGHTS": {"AAPL": 1.0},
}


class ProgressRecorder:
    """Captures ``on_progress`` calls so a test can assert on the sequence."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def __call__(self, pct: int, stage: str) -> None:
        self.calls.append((int(pct), str(stage)))

    @property
    def percentages(self) -> list[int]:
        return [pct for pct, _ in self.calls]


@pytest.fixture(scope="module")
def completed_run(
    tmp_path_factory: pytest.TempPathFactory,
    database_available: tuple[bool, str],
) -> tuple[RunResult, ProgressRecorder]:
    """One real event-mode run of portfolio_dummy, reused by several tests.

    ``database_available`` is requested explicitly rather than left to the
    autouse ``db``-marker fixture: module-scoped fixtures are set up before
    function-scoped ones, so without this the run would execute in full and
    only *then* be told the database is unreachable.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)

    progress = ProgressRecorder()
    artifact_dir = tmp_path_factory.mktemp("run_single_completed")
    request = RunRequest(
        run_id=str(uuid.uuid4()),
        strategy_key="portfolio_dummy",
        class_path=DUMMY_CLASS_PATH,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_capital=100_000.0,
        mode="event",
        params=dict(FAST_PARAMS),
        artifact_dir=str(artifact_dir),
        on_progress=progress,
    )
    return run_single(request), progress


def test_run_completes(completed_run) -> None:
    result, _ = completed_run
    assert result.status == "completed", result.error
    assert result.error is None


def test_progress_is_monotonic_and_ends_at_100(completed_run) -> None:
    _, progress = completed_run
    percentages = progress.percentages
    assert percentages, "no progress was reported at all"
    assert percentages == sorted(percentages), f"progress went backwards: {percentages}"
    assert percentages[0] == 0
    assert percentages[-1] == 100
    assert all(0 <= pct <= 100 for pct in percentages)
    assert any(stage for _, stage in progress.calls), "stages must be labelled"


def test_equity_curve_is_populated_and_consistent(completed_run) -> None:
    result, _ = completed_run
    assert result.equity_curve, "an equity curve is the whole point"
    days = [point.date for point in result.equity_curve]
    assert days == sorted(days), "equity curve must be chronological"
    assert all(point.equity > 0 for point in result.equity_curve)
    assert result.final_equity == pytest.approx(result.equity_curve[-1].equity)


def test_metrics_have_every_run_metrics_key(completed_run) -> None:
    result, _ = completed_run
    assert set(result.metrics) == set(METRIC_KEYS)
    # The six the engine computes are numbers; the three round-trip metrics are
    # left to the caller's trade pairing and must be explicitly absent.
    for key in ("total_return", "cagr", "sharpe", "sortino", "max_drawdown", "volatility"):
        assert isinstance(result.metrics[key], float), key
    for key in ("win_rate", "profit_factor", "total_trades"):
        assert result.metrics[key] is None, key


def test_fills_are_raw_executor_records(completed_run) -> None:
    result, _ = completed_run
    assert isinstance(result.fills, list)
    for fill in result.fills:
        assert {"timestamp", "ticker", "signal_type", "shares", "fill_price"} <= set(fill)


def test_artifacts_are_written_into_the_requested_directory(completed_run) -> None:
    result, _ = completed_run
    assert result.artifact_dir is not None
    csvs = list(Path(result.artifact_dir).rglob("*.csv"))
    assert csvs, "the engine's report CSVs should land under the run's artifact dir"


def test_unknown_ticker_fails_and_names_it(tmp_path: Path) -> None:
    """An empty window must fail loudly, never succeed with an empty curve."""
    fake = "ZZZZFAKE"
    request = RunRequest(
        run_id=str(uuid.uuid4()),
        strategy_key="portfolio_dummy",
        class_path=DUMMY_CLASS_PATH,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_capital=100_000.0,
        mode="event",
        # Order matters: the fake universe has to win over FAST_PARAMS' own
        # TICKERS/WEIGHTS entries.
        params={**FAST_PARAMS, "TICKERS": [fake], "WEIGHTS": {fake: 1.0}},
        artifact_dir=str(tmp_path / "fake_ticker"),
    )

    result = run_single(request)

    assert result.status == "failed"
    assert result.error is not None
    assert fake in result.error
    assert "NoMarketData" in result.error
    assert result.equity_curve == []
    assert result.final_equity is None


def test_cancellation_before_the_run_starts_is_immediate(tmp_path: Path) -> None:
    """A run cancelled while queued must not pay for data or indicator warm-up."""
    progress = ProgressRecorder()
    request = RunRequest(
        run_id=str(uuid.uuid4()),
        strategy_key="portfolio_dummy",
        class_path=DUMMY_CLASS_PATH,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_capital=100_000.0,
        mode="event",
        params=dict(FAST_PARAMS),
        artifact_dir=str(tmp_path / "cancelled_early"),
        on_progress=progress,
        should_cancel=lambda: True,
    )

    result = run_single(request)

    assert result.status == "cancelled"
    assert result.equity_curve == []
    assert progress.percentages == [0], (
        "an already-cancelled run should stop before reporting simulation progress"
    )


def test_cancellation_during_simulation_stops_the_run(tmp_path: Path) -> None:
    """Cancel once the event loop is actually stepping through timestamps."""
    progress = ProgressRecorder()

    def should_cancel() -> bool:
        return any(stage == "simulating" for _, stage in progress.calls)

    request = RunRequest(
        run_id=str(uuid.uuid4()),
        strategy_key="portfolio_dummy",
        class_path=DUMMY_CLASS_PATH,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_capital=100_000.0,
        mode="event",
        params=dict(FAST_PARAMS),
        artifact_dir=str(tmp_path / "cancelled"),
        on_progress=progress,
        should_cancel=should_cancel,
    )

    result = run_single(request)

    assert result.status == "cancelled"
    assert result.error is not None
    assert "cancel" in result.error.lower()
    # A cancelled run reports no results: partial metrics would be read as real.
    assert result.metrics == {}
    assert result.equity_curve == []
    assert 100 not in progress.percentages, "a cancelled run never reaches 100%"


def test_fast_mode_is_refused_when_no_vector_adapter_exists(tmp_path: Path) -> None:
    """Fast mode must fail immediately, not after a run's worth of data loading.

    ``portfolio_dummy`` has no entry in ``ADAPTERS_BY_CLASSNAME``, so fast mode
    cannot produce anything. Until the submission endpoint rejects the mode at
    validation time, this is what keeps a mistaken choice from occupying a
    worker slot to no purpose. Needs no database despite the module marker: the
    refusal happens before the first query.
    """
    from engine.analytics.vector_strategy_adapters import ADAPTERS_BY_CLASSNAME
    from engine.run_single import fast_mode_supported, load_strategy_class

    strategy_class = load_strategy_class(DUMMY_CLASS_PATH)
    assert not fast_mode_supported(strategy_class)

    progress = ProgressRecorder()
    request = RunRequest(
        run_id=str(uuid.uuid4()),
        strategy_key="portfolio_dummy",
        class_path=DUMMY_CLASS_PATH,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_capital=100_000.0,
        mode="fast",
        params=dict(FAST_PARAMS),
        artifact_dir=str(tmp_path / "fast_unsupported"),
        on_progress=progress,
    )

    result = run_single(request)

    assert result.status == "failed"
    assert result.error is not None
    # The message has to name the way out, not just the problem: "ValueError:
    # invalid literal for int()" is what this used to say.
    assert "Fast mode is not available" in result.error
    assert "event mode" in result.error
    assert progress.calls == [], "nothing should have started"
    # And the check is a real lookup, not a hardcoded refusal.
    assert set(ADAPTERS_BY_CLASSNAME) == {
        "VolMomentum",
        "MomentumStrategy",
        "RegimeAdaptiveStrategy",
        "TrendRotateStrategy",
    }
