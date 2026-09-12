"""Application reporting over daily marks; never changes strategy execution.

Risk statistics match the frontend's daily-series convention (sample standard
deviation, 252 periods, 2% annual risk-free rate). Legacy engine statistics are
retained separately by the worker. Undefined statistics stay null in storage;
the legacy numeric API carries an explicit availability map alongside them.
"""

from __future__ import annotations

import math
from statistics import fmean, stdev
from typing import Any, Mapping, Sequence

from src.services.trade_pairing import TradeRow

REPORT_VERSION = 1
PERIODS_PER_YEAR = 252
RISK_FREE_RATE = 0.02


def daily_metrics(
    equity: Sequence[float], initial_capital: float
) -> dict[str, float | None]:
    """Whole-run return uses starting capital; risk uses observed daily closes.

    No synthetic pre-run date or weekend observations are inserted. CAGR, like
    the frontend's sliced-window calculation, is first-observation to last over
    (n-1)/252 years. The capital-based total return includes first-session P&L.
    """
    if not equity or not all(math.isfinite(x) for x in equity):
        raise ValueError("A completed report needs a finite daily equity series.")
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("Report initial capital must be finite and positive.")
    returns = [
        current / previous - 1
        for previous, current in zip(equity, equity[1:])
        if previous != 0
    ]
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1 if peak else 0.0)
    cagr = None
    if len(equity) >= 2 and equity[0] > 0 and equity[-1] >= 0:
        try:
            cagr = (equity[-1] / equity[0]) ** (
                PERIODS_PER_YEAR / (len(equity) - 1)
            ) - 1
        except OverflowError:
            pass
    sharpe = sortino = volatility = None
    if len(returns) >= 2 and all(math.isfinite(x) for x in returns):
        excess = [x - RISK_FREE_RATE / PERIODS_PER_YEAR for x in returns]
        deviation = stdev(excess)
        volatility = stdev(returns) * math.sqrt(PERIODS_PER_YEAR)
        sharpe = (
            fmean(excess) / deviation * math.sqrt(PERIODS_PER_YEAR)
            if deviation
            else None
        )
        downside = [x * x for x in excess if x < 0]
        downside_deviation = math.sqrt(fmean(downside)) if downside else 0.0
        sortino = (
            fmean(excess) / downside_deviation * math.sqrt(PERIODS_PER_YEAR)
            if downside_deviation
            else None
        )
    return {
        key: (
            value
            if value is not None and math.isfinite(value) and abs(value) < 1e10
            else None
        )
        for key, value in {
            "total_return": equity[-1] / initial_capital - 1,
            "cagr": cagr,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_drawdown": drawdown,
            "volatility": volatility,
        }.items()
    }


def open_positions(
    trades: Sequence[TradeRow], final_prices: Mapping[str, float]
) -> list[dict[str, Any]]:
    """Mark still-open lots separately from their zero *realised* trade P&L."""
    positions = []
    for trade in trades:
        if trade.exit_date is not None:
            continue
        mark = final_prices.get(trade.symbol)
        if mark is not None and (not math.isfinite(mark) or mark <= 0):
            mark = None
        sign = -1 if trade.side == "short" else 1
        positions.append(
            {
                "tradeSeq": trade.seq,
                "symbol": trade.symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "entryPrice": trade.entry_price,
                "markPrice": mark,
                "unrealizedPnl": (
                    sign * (mark - trade.entry_price) * trade.quantity
                    if mark is not None
                    else None
                ),
                "marketValue": (
                    sign * mark * trade.quantity if mark is not None else None
                ),
                "fees": trade.fees,
            }
        )
    return positions


def calculation_metadata() -> dict[str, Any]:
    return {
        "reportVersion": REPORT_VERSION,
        "frequency": "daily_last_observation",
        "periodsPerYear": PERIODS_PER_YEAR,
        "annualRiskFreeRate": RISK_FREE_RATE,
        "standardDeviation": "sample",
        "riskReturnBasis": "consecutive_observed_daily_closes",
        "totalReturnBasis": "initial_capital_to_final_equity",
        "cagrBasis": "first_to_last_observed_equity_over_(points-1)/252_years",
        "tradePnlBasis": "realized_gross_of_separately_reported_fees",
        "openPositionPnlBasis": "unrealized_gross_of_separately_reported_fees",
    }
