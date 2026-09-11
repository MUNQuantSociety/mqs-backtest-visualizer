"""Read-only dashboard windows, with one end date shared across strategies."""

from calendar import monthrange
from datetime import date
import logging

from src.schemas.backtests import BacktestDetail, BacktestEquity, EquityWindow, LookbackPeriod

logger = logging.getLogger(__name__)


def window_equity(detail: BacktestDetail, period: LookbackPeriod, end: date) -> BacktestEquity:
    start = None
    if period != "max":
        year = end.year - int(period[0])
        start = end.replace(year=year, day=min(end.day, monthrange(year, end.month)[1]))
    first = start.isoformat() if start else None
    last = end.isoformat()
    points = sorted(detail.equity_curve, key=lambda point: point.date)
    selected = [p for p in points if (first is None or p.date >= first) and p.date <= last]
    logger.info(
        "DASHBOARD | Equity window; run=%s period=%s requested=%s..%s points=%s/%s actual=%s..%s",
        detail.id, period, first or "all", last, len(selected), len(points),
        selected[0].date if selected else "none", selected[-1].date if selected else "none",
    )
    return BacktestEquity(
        id=detail.id, strategy_id=detail.strategy_id, symbol=detail.symbol,
        equity_curve=selected,
        window=EquityWindow(
            period=period, requested_start=first, requested_end=last,
            available_start=points[0].date if points else None,
            available_end=points[-1].date if points else None,
        ),
    )
