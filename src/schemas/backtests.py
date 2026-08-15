"""Backtest response models.

Mirrors ``src/features/backtests/types.ts`` in the frontend. Field names and
enum members are part of the contract — the client parses with Zod and rejects
anything that does not match.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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


class BacktestListResponse(Page):
    items: list[BacktestSummary]
