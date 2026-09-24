"""Compute a report in a spawned process without creating a database run row."""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from src.models import BacktestRun, RunEquityPoint, RunMetrics, RunTrade
from src.repositories.runs import RunListRow
from src.schemas.backtests import BacktestDetail
from src.services.run_controls import timeframe_label
from src.workers import run_job as reporting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSpec:
    id: uuid.UUID
    owner_id: uuid.UUID | None
    name: str
    strategy_key: str
    strategy_name: str
    kind: str
    class_path: str | None
    storage_key: str | None
    start_date: date
    end_date: date
    initial_capital: float
    symbol: str
    engine_version: str
    params: dict
    created_at: datetime
    purpose: str = "user"

    def empty_detail(self, status="queued", progress=0, error=None) -> BacktestDetail:
        from src.services.backtests import to_detail

        row = self.transient_row(status)
        row.progress_pct, row.error_message = progress, error
        return to_detail(RunListRow(row, self.strategy_name))

    def transient_row(self, status: str) -> BacktestRun:
        # Reuse the existing report serializer and numeric semantics. This ORM
        # object is never attached to a session or inserted into a database.
        return BacktestRun(
            id=self.id, owner_id=self.owner_id, name=self.name,
            strategy_key=self.strategy_key, purpose=self.purpose, status=status,
            start_date=self.start_date, end_date=self.end_date,
            timeframe=timeframe_label(self.params),
            initial_capital=self.initial_capital, symbol=self.symbol,
            engine_version=self.engine_version, params=dict(self.params),
            created_at=self.created_at, progress_pct=0,
            equity_points=[], trades=[],
        )


class JobControl:
    """Throttled IPC progress/cancellation. No PostgreSQL connection."""

    def __init__(self, state, run_id):
        self.state, self.run_id = state, run_id
        self.last_poll = float("-inf")
        self.cancelled = False
        self.last_progress = None

    def on_progress(self, pct, stage):
        progress = (max(0, min(99, int(pct))), str(stage))
        if progress != self.last_progress:
            self.state.update(progress_pct=progress[0], stage=progress[1])
            logger.info("PROGRESS | run=%s progress=%d%% stage=%s", self.run_id, *progress)
            self.last_progress = progress

    def should_cancel(self):
        now = time.monotonic()
        if now - self.last_poll >= 0.1:
            self.last_poll = now
            self.cancelled = self.cancelled or bool(self.state.get("cancel_requested"))
        return self.cancelled


def build_report(spec: RunSpec, context, result) -> BacktestDetail:
    from src.services.backtests import to_detail

    trades = reporting.pair_fills(result.fills, market_timezone=reporting.settings.market_timezone)
    curve = reporting._equity_rows(spec.id, result.equity_curve)
    if not curve:
        raise ValueError("A completed backtest did not produce an equity curve.")
    final = curve[-1]["equity"]
    if result.final_equity is not None and abs(
        reporting._money_required(result.final_equity, "final_equity") - final
    ) > reporting.Decimal("0.0001"):
        raise ValueError("Engine final equity does not match its final daily observation.")
    metrics = reporting._metrics_row(context, result, trades, len(curve))
    row = spec.transient_row("completed")
    row.metrics = RunMetrics(**metrics)
    row.equity_points = [RunEquityPoint(**point) for point in curve]
    row.trades = [RunTrade(**reporting._trade_row(spec.id, trade)) for trade in trades]
    row.final_equity = final
    for field in ("total_return", "sharpe", "max_drawdown"):
        setattr(row, field, metrics[field])
    row.progress_pct = 100
    detail = to_detail(RunListRow(row, spec.strategy_name))
    detail.report_metadata.update(purpose=spec.purpose, engineVersion=spec.engine_version)
    return detail


def execute_report(spec: RunSpec, state) -> dict:
    """Return a complete report or an error; persist neither from the worker."""
    from src.core.logging_config import configure_logging

    configure_logging(reporting.settings.log_level)
    context = None
    control = JobControl(state, spec.id)
    try:
        if control.should_cancel():
            return {"error": reporting.CANCELLED_MESSAGE}
        state.update(status="running", progress_pct=0)
        class_path, workdir = reporting._resolve_class_path(
            spec.id, spec.strategy_key, spec.kind, spec.class_path, spec.storage_key,
        )
        params = dict(spec.params)
        mode = params.pop(reporting.MODE_KEY, reporting.DEFAULT_MODE)
        context = reporting._RunContext(spec.id, spec.strategy_key, class_path,
            spec.start_date, spec.end_date, spec.initial_capital, mode, params,
            workdir, spec.storage_key)
        source = reporting._strategy_source(context)
        # Record the effective configuration actually supplied to this engine.
        if workdir is not None:
            config_path = workdir / "config.json"
        else:
            from engine.run_single import load_strategy_class
            config_path = Path(inspect.getfile(load_strategy_class(class_path))).parent / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        request = reporting._build_request(context, control)
        config.update(request.params)
        logger.info("ENGINE | Starting transient job; run=%s strategy=%s", spec.id, spec.strategy_key)
        result = reporting.run_single(request)
        if result.status != "completed":
            return {"error": reporting.CANCELLED_MESSAGE if result.status == "cancelled"
                    else result.error or "The backtest failed."}
        if control.should_cancel():
            return {"error": reporting.CANCELLED_MESSAGE}
        result.report_metadata.update(strategySource=source, resolvedConfig=config,
            executionSettings={"mode": mode, "slippage": request.slippage,
                               "commissionPerShare": request.commission_per_share})
        return {"report": build_report(spec, context, result).model_dump(mode="json", by_alias=True)}
    except Exception as exc:
        logger.exception("Run %s failed before a report could be saved", spec.id)
        return {"error": f"{type(exc).__name__}: {exc}"[:2000]}
    finally:
        if context is not None:
            reporting._remove_workdir(context.workdir)
