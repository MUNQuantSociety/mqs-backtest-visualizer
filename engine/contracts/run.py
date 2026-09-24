"""What goes into one backtest run, and what comes back out.

These dataclasses are the contract between the engine and everything above it.
They are plain stdlib types on purpose: the worker builds a ``RunRequest``
inside its own process, and a ``RunResult`` has to survive being handed back
across a process boundary, so nothing here may reference pandas, SQLAlchemy,
or a database session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

if TYPE_CHECKING:
    from engine.core.sentiment_gate import SentimentGate

# The metrics dictionary is keyed exactly like the ``app.run_metrics`` columns
# so persistence is a straight column-by-column write with no translation
# table to drift out of sync.
METRIC_KEYS: tuple[str, ...] = (
    "total_return",
    "cagr",
    "sharpe",
    "sortino",
    "max_drawdown",
    "volatility",
    "win_rate",
    "profit_factor",
    "total_trades",
)


class EquityPoint(NamedTuple):
    """One sample of the equity curve.

    A ``NamedTuple`` so it unpacks as the ``(date, equity, benchmark)`` tuple
    the plan specifies while still being readable at the call site.
    """

    date: date
    equity: float
    benchmark: float | None = None


def _noop_progress(pct: int, stage: str) -> None:
    """Default ``on_progress``: a run with nobody watching still runs."""


def _never_cancel() -> bool:
    """Default ``should_cancel``: nothing to poll, so never cancelled."""
    return False


@dataclass
class RunRequest:
    """One portfolio, one window, one set of parameters.

    ``class_path`` points at the strategy class to execute, either as
    ``"package.module:ClassName"`` or as a plain dotted path whose last
    component is the class. Built-ins live under ``engine.strategies.*``;
    user uploads are imported from a materialized directory and passed in
    the same way.
    """

    run_id: str
    strategy_key: str
    class_path: str
    start_date: str | date
    end_date: str | date
    initial_capital: float
    mode: str = "event"
    params: dict[str, Any] = field(default_factory=dict)
    artifact_dir: str | None = None
    slippage: float = 0.0

    # Injected by the worker: one writes progress to the run row, the other
    # reads the cancellation flag off it. Defaults make the engine usable from
    # a script with no database in sight.
    on_progress: Callable[[int, str], None] = _noop_progress
    should_cancel: Callable[[], bool] = _never_cancel
    # Cash charged per filled share, on both buys and sells. Kept separate
    # from fractional slippage and from the strategy's parameter dictionary.
    commission_per_share: float = 0.0
    # Optional news gate on long entries, with its article scores already
    # loaded by the worker (engine/core/sentiment_gate.py). None is ungated.
    sentiment_gate: "SentimentGate | None" = None


@dataclass
class RunResult:
    """Everything the API layer needs to persist and render a finished run.

    ``status`` is terminal in all three cases; ``error`` is set for every
    status except ``"completed"``.
    """

    status: str  # "completed" | "failed" | "cancelled"
    error: str | None = None
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    final_equity: float | None = None
    artifact_dir: str | None = None
    # The close of the last bar the equity curve saw, per ticker — the marks
    # ``final_equity`` was valued at. Supplemental unrealized P&L can use these
    # marks without inventing closing fills. Last quotes can be stale for a
    # ticker that did not trade at the final timestamp.
    # Empty when there is nothing to mark (fast mode has no fills). A plain
    # dict of floats so it crosses the process boundary like everything else.
    final_prices: dict[str, float] = field(default_factory=dict)
    # Plain serializable provenance for RunMetrics.extra / API reportMetadata.
    # Includes benchmark definition/coverage and the explicit chart baseline.
    report_metadata: dict[str, Any] = field(default_factory=dict)
