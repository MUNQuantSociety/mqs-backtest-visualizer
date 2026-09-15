"""One-time, deletable example reports for a new authenticated user."""

from __future__ import annotations

import logging
import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AppUser
from src.repositories import reports, strategies, users
from src.schemas.backtests import (
    BacktestDetail,
    EquityPoint,
    PerformanceMetrics,
    Trade,
)

logger = logging.getLogger(__name__)

STARTER_STRATEGY_KEYS = ("portfolio_1", "portfolio_2")
STARTER_REPORT_NAMESPACE = uuid.UUID("e50b70e1-60f0-4fc6-a239-ea482d73f333")
STARTER_TRADING_DAYS = 260
STARTER_INITIAL_CAPITAL = 100_000.0


@dataclass(frozen=True)
class _CurveShape:
    drift: float
    phase: float


_CURVE_SHAPES = {
    "portfolio_1": _CurveShape(drift=0.00042, phase=0.4),
    "portfolio_2": _CurveShape(drift=0.00035, phase=1.8),
}


async def ensure_starter_reports(session: AsyncSession, user: AppUser) -> int:
    """Create two examples once, or mark an existing owner as onboarded.

    The caller owns the transaction. Locking the user row makes concurrent
    first requests converge on one decision. The marker and any reports commit
    together, so a failed insert is retried on the next authenticated request.
    """
    if user.starter_reports_seeded_at is not None:
        return 0

    locked_user = await users.lock_user(session, user.id)
    if locked_user.starter_reports_seeded_at is not None:
        return 0

    seeded_at = datetime.now(timezone.utc)
    if await reports.owner_has_visible_reports(session, user.id):
        locked_user.starter_reports_seeded_at = seeded_at
        logger.info(
            "ONBOARDING | Existing report history retained; user=%s",
            user.id,
        )
        return 0

    registry = []
    for key in STARTER_STRATEGY_KEYS:
        strategy = await strategies.get_strategy(session, key)
        if (strategy is None or not strategy.enabled
                or strategy.kind != "builtin" or strategy.status != "active"):
            logger.warning(
                "ONBOARDING | Starter reports deferred; strategy=%s unavailable; user=%s",
                key,
                user.id,
            )
            return 0
        registry.append(strategy)

    details = [
        _build_report(
            owner_id=user.id,
            strategy_key=strategy.key,
            strategy_name=strategy.name,
            universe=list(strategy.universe or []),
            created_at=seeded_at,
        )
        for strategy in registry
    ]
    await reports.add_completed_reports(session, user.id, details)
    locked_user.starter_reports_seeded_at = seeded_at
    logger.info(
        "ONBOARDING | Starter reports created; user=%s strategies=%s",
        user.id,
        ",".join(STARTER_STRATEGY_KEYS),
    )
    return len(details)


def _build_report(
    *,
    owner_id: uuid.UUID,
    strategy_key: str,
    strategy_name: str,
    universe: list[str],
    created_at: datetime,
) -> BacktestDetail:
    shape = _CURVE_SHAPES[strategy_key]
    report_id = uuid.uuid5(
        STARTER_REPORT_NAMESPACE, f"{owner_id}:{strategy_key}"
    )
    dates = _trading_days_ending(created_at.date(), STARTER_TRADING_DAYS)
    equity_curve = _equity_curve(dates, shape)
    trades = _trades(report_id, dates, universe, shape.phase)
    metrics = _metrics(equity_curve, trades)

    return BacktestDetail(
        id=str(report_id),
        name=reports.example_name(strategy_name),
        strategy_id=strategy_key,
        strategy_name=strategy_name,
        symbol="MULTI",
        timeframe="1d",
        status="completed",
        start_date=dates[0].isoformat(),
        end_date=dates[-1].isoformat(),
        created_at=created_at.isoformat().replace("+00:00", "Z"),
        initial_capital=STARTER_INITIAL_CAPITAL,
        final_equity=equity_curve[-1].equity,
        total_return=metrics.total_return,
        sharpe=metrics.sharpe,
        max_drawdown=metrics.max_drawdown,
        metrics=metrics,
        equity_curve=equity_curve,
        trades=trades,
        parameters={
            "mode": "event",
            "universe": universe,
            "LOOKBACK_DAYS": 90,
        },
        progress_pct=100,
        error_message=None,
        report_metadata={
            "purpose": "example",
            "starterExample": {
                "generated": True,
                "deletable": True,
                "message": reports.EXAMPLE_WARNING,
            },
            "marketData": {
                "source": "bundled_starter_example",
                "resolution": "daily",
            },
            "reportVersion": 1,
        },
        open_positions=[],
    )


def _trading_days_ending(end: date, count: int) -> list[date]:
    days = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    days.reverse()
    return days


def _equity_curve(
    dates: list[date], shape: _CurveShape
) -> list[EquityPoint]:
    equity = STARTER_INITIAL_CAPITAL
    benchmark = STARTER_INITIAL_CAPITAL
    points = []
    for index, day in enumerate(dates):
        if index:
            equity *= 1 + shape.drift + 0.0048 * math.sin(
                index * 0.31 + shape.phase
            ) + 0.0017 * math.cos(index * 0.11 + shape.phase)
            benchmark *= 1 + 0.00030 + 0.0039 * math.sin(index * 0.27 + 0.9)
        points.append(
            EquityPoint(
                date=day.isoformat(),
                equity=round(equity, 2),
                benchmark=round(benchmark, 2),
            )
        )
    return points


def _trades(
    report_id: uuid.UUID,
    dates: list[date],
    universe: list[str],
    phase: float,
) -> list[Trade]:
    symbols = universe or ["AAPL", "MSFT"]
    trades = []
    for index in range(6):
        entry_index = 18 + index * 36
        exit_index = entry_index + 7
        entry_price = 90.0 + index * 14.0 + phase * 3.0
        return_pct = 0.012 + 0.035 * math.sin(index * 1.4 + phase)
        exit_price = entry_price * (1 + return_pct)
        quantity = 40.0 + index * 5.0
        trades.append(
            Trade(
                id=f"{report_id}:{index}",
                symbol=symbols[index % len(symbols)],
                side="long",
                entry_date=dates[entry_index].isoformat(),
                exit_date=dates[exit_index].isoformat(),
                entry_price=round(entry_price, 2),
                exit_price=round(exit_price, 2),
                quantity=quantity,
                pnl=round((exit_price - entry_price) * quantity, 2),
                return_pct=return_pct,
                fees=0.0,
            )
        )
    return trades


def _metrics(
    curve: list[EquityPoint], trades: list[Trade]
) -> PerformanceMetrics:
    returns = [
        current.equity / previous.equity - 1
        for previous, current in zip(curve, curve[1:])
    ]
    mean_return = statistics.fmean(returns)
    daily_volatility = statistics.stdev(returns)
    downside = math.sqrt(
        statistics.fmean(min(value, 0.0) ** 2 for value in returns)
    )
    annualization = math.sqrt(252)
    risk_free_daily = 0.02 / 252
    total_return = curve[-1].equity / STARTER_INITIAL_CAPITAL - 1
    years = (len(curve) - 1) / 252

    peak = STARTER_INITIAL_CAPITAL
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        max_drawdown = min(max_drawdown, point.equity / peak - 1)

    profits = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl <= 0]
    return PerformanceMetrics(
        total_return=total_return,
        cagr=(curve[-1].equity / STARTER_INITIAL_CAPITAL) ** (1 / years) - 1,
        sharpe=(mean_return - risk_free_daily)
        / daily_volatility
        * annualization,
        sortino=(mean_return - risk_free_daily) / downside * annualization,
        max_drawdown=max_drawdown,
        volatility=daily_volatility * annualization,
        win_rate=len(profits) / len(trades),
        profit_factor=sum(profits) / abs(sum(losses)),
        total_trades=len(trades),
        unavailable={},
    )
