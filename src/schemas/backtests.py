"""Backtest request and response models.

Mirrors ``src/features/backtests/types.ts`` in the frontend. Field names and
enum members are part of the contract — the client parses with Zod and rejects
anything that does not match.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field

from src.schemas.common import CamelModel, Page


class BacktestStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EquityPoint(CamelModel):
    date: str
    equity: float
    benchmark: float | None = None


class Trade(CamelModel):
    id: str
    symbol: str
    side: Literal["long", "short"]
    entry_date: str
    exit_date: str | None
    entry_price: float
    exit_price: float | None
    quantity: float
    pnl: float
    return_pct: float
    fees: float = 0.0


class PerformanceMetrics(CamelModel):
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    volatility: float
    win_rate: float
    profit_factor: float
    total_trades: int


class BacktestSummary(CamelModel):
    """List-row shape — deliberately lighter than the detail payload."""

    id: str
    name: str
    strategy_id: str
    strategy_name: str
    symbol: str
    timeframe: str
    status: BacktestStatus
    start_date: str
    end_date: str
    created_at: str
    initial_capital: float
    final_equity: float
    total_return: float
    sharpe: float
    max_drawdown: float


class BacktestDetail(BacktestSummary):
    metrics: PerformanceMetrics
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    parameters: dict[str, Any] = {}
    # How far a running backtest has got, 0-100. Additive field: the client's
    # Zod schema ignores keys it does not declare, so shipping it ahead of the
    # UI that renders it is safe. Nullable because "no idea" is a real answer
    # for a run whose worker has not reported yet.
    progress_pct: int | None = None
    # Why a failed run failed, in one sentence a student can act on. Also
    # additive, and the other half of the cancellation contract promised to the
    # frontend session: a cancelled run is `status="failed"` with
    # `errorMessage="Cancelled by user"`, which is unreadable without this.
    error_message: str | None = None


class BacktestListResponse(Page):
    items: list[BacktestSummary]


class BacktestRunRequest(CamelModel):
    """What the New Run form posts to ``POST /backtests``.

    Deliberately loose about types. Dates arrive as strings and capital as a
    plain number so that everything a student can get wrong — a malformed
    date, a backwards window, a parameter outside its range — is rejected by
    the service with one readable sentence in ``detail``. FastAPI's own
    validation answers with a *list* of error objects instead, and the client's
    error reader only understands a string, so a 422 raised by Pydantic here
    would reach the browser as "Request failed with status code 422".
    """

    name: str
    strategy_key: str
    start_date: str
    end_date: str
    initial_capital: float
    # Only the event loop is dependable across every vendored strategy; the
    # vectorised path exists but not every strategy implements it, and the
    # engine says so per run.
    mode: str = "event"
    # Overlaid on the strategy's ``config.json`` at run time. Validated against
    # the strategy's ``param_specs`` — an unknown key is a typo, not a feature.
    params: dict[str, Any] = Field(default_factory=dict)
