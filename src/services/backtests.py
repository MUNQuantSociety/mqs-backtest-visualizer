"""Backtest submission, transient polling, and completed JSON report retrieval."""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

# A constant, not the engine: ``engine/__init__.py`` imports nothing, so
# stamping a run with the code that will execute it costs no pandas import.
from engine import ENGINE_VERSION
from engine.data.fmp import FMPUnavailable as FMPUnavailable  # re-exported to the API routes
from engine.data.bar_interval import IntradayWindowTooLarge, bar_minutes, check_intraday_size
from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.models import BacktestRun, RunEquityPoint, RunMetrics, RunTrade
from src.repositories import reports as reports_repo
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.repositories.runs import RunListRow
from src.services import market_data as market_data_service
from src.services.run_controls import split_controls
from src.schemas.backtests import (
    BacktestDetail,
    BacktestListResponse,
    BacktestRunRequest,
    BacktestStatus,
    BacktestSummary,
    EquityPoint,
    PerformanceMetrics,
    Trade,
)

logger = logging.getLogger(__name__)

# New application submissions use real event execution. Existing fast-mode
# reports and the standalone engine remain readable and executable.
RUN_MODES = ("event",)

# Long enough for "Regime adaptive — 2025 H1 with a 90 day lookback", short
# enough that a run name stays a label rather than a paragraph pasted into a
# list column.
NAME_LIMIT = 120

# Reserved key inside ``backtest_runs.params``. The params column is an overlay
# on the strategy's ``config.json``, whose keys are all upper case, so a
# lower-case ``mode`` does not collide with any parameter anyone has written.
# "Nobody has written one" is a convention rather than a guarantee, though, and
# the overlay would silently win over a parameter of the same name — so the key
# is refused at validation instead of being allowed to overwrite anything. The
# worker pops it back off before handing the overlay to the engine — see
# ``src/workers/run_job.py``.
MODE_KEY = "mode"

# ``params`` values the client may send, by ``ParameterSpec.type``. Booleans
# are checked separately: in Python ``bool`` is an ``int``, so a spec of type
# "number" would silently accept ``true`` as 1 without this split.
_NUMERIC_SPEC_TYPES = frozenset({"number", "integer", "percent"})


class DeleteOutcome(str, Enum):
    """What ``DELETE /backtests/{id}`` actually did.

    Saved reports are deleted; unfinished jobs receive a cancellation flag.
    Both answer 204; the route needs the distinction only for its 404 case.
    """

    DELETED = "deleted"
    CANCEL_REQUESTED = "cancel_requested"
    NOT_FOUND = "not_found"


class RunSubmissionError(ValueError):
    """A submission the student can fix, carrying the sentence to show them.

    The route turns this into a 422 whose ``detail`` is ``str(exc)`` verbatim,
    so every message here is written to be read in a form's error slot: it says
    which field is wrong, what was sent, and what would be accepted.
    """


def _float(value: Decimal | float | None, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def _optional_float(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _iso_datetime(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.isoformat().replace("+00:00", "Z")


def _iso_date(day: date | None) -> str:
    return day.isoformat() if day is not None else ""


def _to_summary(row: RunListRow) -> BacktestSummary:
    run = row.run
    return BacktestSummary(
        id=str(run.id),
        name=run.name,
        strategy_id=run.strategy_key,
        strategy_name=row.strategy_name,
        symbol=run.symbol,
        timeframe=run.timeframe,
        status=BacktestStatus(run.status),
        start_date=_iso_date(run.start_date),
        end_date=_iso_date(run.end_date),
        created_at=_iso_datetime(run.created_at),
        initial_capital=_float(run.initial_capital),
        final_equity=_float(run.final_equity),
        total_return=_float(run.total_return),
        sharpe=_float(run.sharpe),
        max_drawdown=_float(run.max_drawdown),
    )


def _to_metrics(metrics: RunMetrics | None) -> PerformanceMetrics:
    """Zeros for a run with no metrics row yet — see the module docstring."""
    if metrics is None:
        return PerformanceMetrics(
            total_return=0.0,
            cagr=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=0.0,
            volatility=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            total_trades=0,
            unavailable=_metric_availability(None),
        )
    return PerformanceMetrics(
        total_return=_float(metrics.total_return),
        cagr=_float(metrics.cagr),
        sharpe=_float(metrics.sharpe),
        sortino=_float(metrics.sortino),
        max_drawdown=_float(metrics.max_drawdown),
        volatility=_float(metrics.volatility),
        win_rate=_float(metrics.win_rate),
        profit_factor=_float(metrics.profit_factor),
        total_trades=int(metrics.total_trades or 0),
        unavailable=_metric_availability(metrics),
    )


def _metric_availability(metrics: RunMetrics | None) -> dict[str, str]:
    unavailable = {}
    for column, field in PerformanceMetrics.model_fields.items():
        if column in {"total_trades", "unavailable"}:
            continue
        if metrics is None or getattr(metrics, column) is None:
            reason = "Undefined for this run's observations or closed trades."
            if metrics is None:
                reason = "Run has no completed metrics yet."
            elif column == "profit_factor":
                reason = "No losing closed trades; profit factor is undefined."
            elif column == "win_rate":
                reason = "No closed trades."
            unavailable[field.alias or column] = reason
    return unavailable


def _to_equity_point(point: RunEquityPoint) -> EquityPoint:
    return EquityPoint(
        date=_iso_date(point.date),
        equity=_float(point.equity),
        benchmark=_optional_float(point.benchmark),
    )


def _to_trade(run: BacktestRun, trade: RunTrade) -> Trade:
    return Trade(
        # Round trips have no identity of their own in the database — the
        # composite key is (run, seq), and the client needs a single string.
        id=f"{run.id}:{trade.seq}",
        symbol=trade.symbol,
        side="short" if trade.side == "short" else "long",
        entry_date=_iso_date(trade.entry_date),
        exit_date=trade.exit_date.isoformat() if trade.exit_date else None,
        entry_price=_float(trade.entry_price),
        exit_price=_optional_float(trade.exit_price),
        quantity=_float(trade.quantity),
        pnl=_float(trade.pnl),
        return_pct=_float(trade.return_pct),
        fees=_float(trade.fees),
    )


def to_detail(row: RunListRow) -> BacktestDetail:
    """Full run payload: summary fields plus metrics, curve, and trades."""
    run = row.run
    summary = _to_summary(row)
    extra = dict(run.metrics.extra or {}) if run.metrics else {}
    positions = extra.pop("openPositions", [])
    return BacktestDetail(
        **summary.model_dump(),
        metrics=_to_metrics(run.metrics),
        equity_curve=[_to_equity_point(point) for point in run.equity_points],
        trades=[_to_trade(run, trade) for trade in run.trades],
        parameters=dict(run.params or {}),
        progress_pct=run.progress_pct,
        error_message=run.error_message,
        report_metadata=extra,
        open_positions=list(positions),
    )


async def list_backtests(
    *, owner_id: uuid.UUID, search: str | None = None,
    status: BacktestStatus | None = None, strategy_id: str | None = None,
    page: int = 1, page_size: int = 25,
) -> BacktestListResponse:
    """Saved successful reports only. Live jobs never enter history."""
    if status is not None and status != BacktestStatus.COMPLETED:
        return BacktestListResponse(items=[], total=0, page=page, page_size=page_size)
    await ensure_schema()
    async with session_scope() as session:
        items, total = await reports_repo.list_reports(
            session, owner_id, search=search, strategy_key=strategy_id,
            page=page, page_size=page_size,
        )
    return BacktestListResponse(items=items, total=total, page=page, page_size=page_size)


async def list_example_backtests(
    *, owner_id: uuid.UUID, search: str | None = None,
    status: BacktestStatus | None = None, strategy_id: str | None = None,
    page: int = 1, page_size: int = 25,
) -> BacktestListResponse:
    """List owned simulations separately from real performance history."""
    if status is not None and status != BacktestStatus.COMPLETED:
        return BacktestListResponse(items=[], total=0, page=page, page_size=page_size)
    await ensure_schema()
    async with session_scope() as session:
        items, total = await reports_repo.list_reports(
            session, owner_id, search=search, strategy_key=strategy_id,
            page=page, page_size=page_size, examples=True,
        )
    return BacktestListResponse(items=items, total=total, page=page, page_size=page_size)


async def list_live_backtests(*, owner_id: uuid.UUID) -> list[BacktestSummary]:
    """This owner's runs still queued or running; empty when no worker pool runs.

    Without a job manager nothing can be in flight, so an empty list is the
    truth rather than an outage to report.
    """
    from src.workers.job_manager import get_job_manager

    try:
        manager = get_job_manager()
    except RuntimeError:
        return []
    return await asyncio.to_thread(manager.live_summaries, owner_id)


async def get_backtest(run_id: str, *, owner_id: uuid.UUID | None = None) -> BacktestDetail | None:
    """Poll transient execution or retrieve this owner's completed JSON report."""
    from src.workers.job_manager import get_job_manager

    parsed = runs_repo.parse_run_id(run_id)
    if parsed is None:
        return None
    try:
        manager = get_job_manager()
    except RuntimeError:
        manager = None
    if manager is not None:
        detail = await asyncio.to_thread(manager.get_detail, parsed, owner_id)
        if detail is not None:
            return detail
    if owner_id is None:
        return None
    await ensure_schema()
    async with session_scope() as session:
        report = await reports_repo.get(session, parsed, owner_id)
        return reports_repo.to_detail(report) if report is not None else None


def _remove_artifacts(run_id: uuid.UUID) -> None:
    shutil.rmtree(settings.artifact_dir / str(run_id), ignore_errors=True)


async def delete_backtest(run_id: str, *, owner_id: uuid.UUID | None = None) -> DeleteOutcome:
    from src.workers.job_manager import get_job_manager

    parsed = runs_repo.parse_run_id(run_id)
    if parsed is None:
        return DeleteOutcome.NOT_FOUND
    try:
        manager = get_job_manager()
    except RuntimeError:
        manager = None
    if manager is not None:
        outcome = await asyncio.to_thread(manager.cancel, parsed, owner_id)
        if outcome == "cancel_requested":
            return DeleteOutcome.CANCEL_REQUESTED
        if outcome == "deleted":
            # A failed or cancelled job saved no report, but its worker may
            # have written artifacts; nothing else will ever remove them.
            _remove_artifacts(parsed)
            return DeleteOutcome.DELETED
    if owner_id is None:
        return DeleteOutcome.NOT_FOUND
    await ensure_schema()
    async with session_scope() as session:
        removed = await reports_repo.remove(session, parsed, owner_id)
    if removed:
        _remove_artifacts(parsed)
        return DeleteOutcome.DELETED
    return DeleteOutcome.NOT_FOUND


async def create_backtest_run(
    *, name: str, strategy_key: str, start_date: date, end_date: date,
    initial_capital: float, symbol: str, engine_version: str,
    params: dict | None = None, purpose: str = "user",
    owner_id: uuid.UUID | None = None,
) -> BacktestSummary:
    """Prepare a job in memory. No run/report database row is inserted."""
    from src.workers.job_manager import get_job_manager
    from src.workers.report_job import RunSpec

    await ensure_schema()
    async with session_scope() as session:
        strategy = await strategies_repo.get_strategy(session, strategy_key)
        if strategy is None:
            raise RunSubmissionError("The selected strategy no longer exists.")
        spec = RunSpec(
            id=uuid.uuid4(), owner_id=owner_id, name=name, strategy_key=strategy_key,
            strategy_name=strategy.name, kind=strategy.kind,
            class_path=strategy.class_path, storage_key=strategy.storage_key,
            start_date=start_date, end_date=end_date, initial_capital=initial_capital,
            symbol=symbol, engine_version=engine_version, params=dict(params or {}),
            created_at=datetime.now(timezone.utc), purpose=purpose,
        )
    await asyncio.to_thread(get_job_manager().register, spec)
    return BacktestSummary.model_validate(spec.empty_detail().model_dump())


# ---------------------------------------------------------------------------
# Submission — POST /backtests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunnableStrategy:
    """The registry facts a submission is checked against.

    A plain snapshot rather than the ORM row: validation and the messages it
    produces happen after the session has closed, and a detached instance that
    lazy-loads is a failure mode nobody needs here.
    """

    key: str
    universe: list[str]
    param_specs: list[dict[str, Any]]


async def _load_runnable_strategy(strategy_key: str) -> _RunnableStrategy:
    """The strategy this run names, or a message explaining why it cannot run."""
    key = (strategy_key or "").strip()
    if not key:
        raise RunSubmissionError("strategyKey is required — pick a strategy to run.")

    await ensure_schema()
    async with session_scope() as session:
        strategy = await strategies_repo.get_strategy(session, key)
        if strategy is None:
            raise RunSubmissionError(
                f"There is no strategy named {key!r}. "
                "Pick one from the catalogue at GET /api/strategies."
            )
        if not strategy.enabled:
            raise RunSubmissionError(_unavailable_reason(strategy.status, key))
        snapshot = _RunnableStrategy(
            key=strategy.key,
            universe=list(strategy.universe or []),
            param_specs=list(strategy.param_specs or []),
        )
        storage_key = strategy.storage_key
    if settings.strategy_store_backend == "s3":
        from src.services.strategy_availability import package_available

        if not await package_available(storage_key):
            raise RunSubmissionError(
                f"Strategy {key!r} has no complete package in the configured "
                "S3 store. Publish or restore it before running a backtest."
            )
    return snapshot


def _unavailable_reason(status: str, key: str) -> str:
    """Why a disabled strategy cannot be run, in the student's terms.

    ``enabled`` is the gate, but on its own it explains nothing: an upload that
    is still validating and an upload whose validation failed are both disabled
    and need completely different responses from the person reading this.
    """
    if status == "validating":
        return (
            f"Strategy {key!r} is still being validated. "
            "It becomes runnable when its validation backtest passes."
        )
    if status == "failed_validation":
        return (
            f"Strategy {key!r} failed validation and cannot be run. "
            "Open its validation run to see the error, then upload a fix."
        )
    if status == "archived":
        return f"Strategy {key!r} is archived and no longer accepts new runs."
    return f"Strategy {key!r} is not available to run."


def _validated_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise RunSubmissionError("Give the run a name so you can find it later.")
    if len(name) > NAME_LIMIT:
        raise RunSubmissionError(
            f"The run name is {len(name)} characters; keep it to {NAME_LIMIT} or fewer."
        )
    return name


def _validated_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise RunSubmissionError(
            f"{field} must be an ISO date like 2025-01-02; got {raw!r}."
        ) from None


def _validated_window(request: BacktestRunRequest) -> tuple[date, date]:
    """Parse both dates and check the window is one the engine can run."""
    start = _validated_date(request.start_date, "startDate")
    end = _validated_date(request.end_date, "endDate")

    if start >= end:
        raise RunSubmissionError(
            f"startDate must be before endDate; got {start.isoformat()} "
            f"to {end.isoformat()}."
        )

    span = (end - start).days
    limit = settings.max_backtest_window_days
    if span > limit:
        raise RunSubmissionError(
            f"That window is {span} days long; the maximum is {limit}. "
            "Pick a shorter range."
        )
    return start, end


def _validated_intraday_size(
    controls: dict[str, Any], universe: list[str], start: date, end: date
) -> None:
    """Refuse an intraday window too large to run, before it is queued.

    The engine repeats the check with the strategy's lookback included; this
    one catches the common case while the student is still on the form.
    """
    bar_seconds = controls.get("BAR_INTERVAL_SECONDS")
    if bar_seconds is None:
        return
    try:
        check_intraday_size(len(universe), start, end, bar_minutes(bar_seconds))
    except IntradayWindowTooLarge as exc:
        raise RunSubmissionError(str(exc)) from None


async def _validated_coverage(universe: list[str], start: date, end: date) -> None:
    """Refuse a window the universe has no prices for.

    Market data ends weeks behind the calendar, so a window that looks
    reasonable can contain no bars at all. Without this the run is accepted,
    queued, executed, and fails deep in the engine with an error about empty
    data, which reads as a broken strategy rather than a bad date.

    Reuses the coverage service so there is one definition of a valid window,
    shared with ``GET /market-data/coverage`` and therefore with the run form's
    date picker. A universe with no tickers is skipped rather than guessed at.
    """
    if not universe:
        return

    if market_data_service.market_data_source() == "fmp":
        try:
            validation = await market_data_service.validate_tickers(universe)
        except ValueError as exc:
            raise RunSubmissionError(str(exc)) from None
        if validation.unknown:
            raise RunSubmissionError(
                f"FMP does not recognize these ticker symbols: {', '.join(validation.unknown)}. "
                "Check the spelling and exchange suffix."
            )
    coverage = await market_data_service.coverage_for(universe)

    if coverage.missing:
        raise RunSubmissionError(
            f"There is no available market-data history for {', '.join(coverage.missing)}. "
            "Choose tickers with historical prices before submitting a run."
        )
    if coverage.start is None or coverage.end is None:
        raise RunSubmissionError(
            f"There is no shared market-data window for {', '.join(universe)}. "
            "Choose tickers with overlapping history."
        )

    if start.isoformat() < coverage.start or end.isoformat() > coverage.end:
        raise RunSubmissionError(
            f"There is only data from {coverage.start} to {coverage.end} for "
            f"{', '.join(universe)}. Pick a window inside that range."
        )


def _validated_capital(raw: float) -> float:
    """Capital has to be positive and finite — it divides every return."""
    capital = float(raw)
    if not math.isfinite(capital):
        raise RunSubmissionError(
            f"initialCapital must be a finite number; got {raw!r}."
        )
    if capital <= 0:
        raise RunSubmissionError(
            f"initialCapital must be greater than zero; got {capital:g}."
        )
    return capital


def _validated_mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    if mode not in RUN_MODES:
        accepted = " or ".join(repr(value) for value in RUN_MODES)
        raise RunSubmissionError(f"mode must be {accepted}; got {raw!r}.")
    return mode


def _validated_params(
    strategy: _RunnableStrategy, submitted: dict[str, Any]
) -> dict[str, Any]:
    """Check the overlay against the strategy's own parameter specs.

    The specs are the same document the catalogue endpoint hands the client to
    build its form from, so anything rejected here is something the form should
    not have been able to send — which is why the message names the key: either
    the request was hand-rolled or the form is out of date.
    """
    if not submitted:
        return {}

    specs = {
        str(spec.get("key")): spec
        for spec in strategy.param_specs
        if isinstance(spec, dict) and spec.get("key")
    }

    validated: dict[str, Any] = {}
    for key, value in submitted.items():
        if key == MODE_KEY:
            # The stored overlay carries the execution mode under this exact
            # key, so accepting a parameter of the same name would mean writing
            # a validated value and then overwriting it — the run would use the
            # mode and the strategy would never see its parameter. Refused
            # rather than renamed, because a seeded spec named ``mode`` is a
            # bug in the seed and silently ignoring it hides that.
            raise RunSubmissionError(
                f"{MODE_KEY!r} is reserved for the run's execution mode and "
                "cannot be sent as a strategy parameter; use the top-level "
                f"{MODE_KEY!r} field instead."
            )
        spec = specs.get(key)
        if spec is None:
            raise RunSubmissionError(_unknown_param_message(key, strategy, specs))
        validated[key] = _validated_param_value(key, value, spec)
    return validated


def _unknown_param_message(
    key: str, strategy: _RunnableStrategy, specs: dict[str, dict[str, Any]]
) -> str:
    if not specs:
        return f"Strategy {strategy.key!r} takes no parameters, but {key!r} was sent."
    accepted = ", ".join(sorted(specs))
    return (
        f"{key!r} is not a parameter of strategy {strategy.key!r}. "
        f"Accepted parameters: {accepted}."
    )


def _validated_param_value(key: str, value: Any, spec: dict[str, Any]) -> Any:
    """One parameter against one spec: type first, then range."""
    spec_type = str(spec.get("type", "number")).lower()

    if spec_type == "boolean":
        if not isinstance(value, bool):
            raise RunSubmissionError(
                f"Parameter {key!r} must be true or false; got {value!r}."
            )
        # A boolean has no range, and a min/max on one would be meaningless.
        return value

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunSubmissionError(f"Parameter {key!r} must be a number; got {value!r}.")
    if not math.isfinite(float(value)):
        raise RunSubmissionError(
            f"Parameter {key!r} must be a finite number; got {value!r}."
        )

    if spec_type == "integer":
        # JSON has one number type, so 90 and 90.0 are the same value to a
        # client that did arithmetic on the way here. Only a real fraction is
        # a mistake worth refusing.
        if float(value) != int(value):
            raise RunSubmissionError(
                f"Parameter {key!r} must be a whole number; got {value!r}."
            )
        value = int(value)
    elif spec_type not in _NUMERIC_SPEC_TYPES:
        # An unrecognised spec type is a seeding bug, not the student's
        # problem: range-check the number rather than refuse a legal request.
        logger.warning(
            "Parameter spec %r declares unknown type %r; treating it as a number",
            key,
            spec_type,
        )

    _check_param_range(key, value, spec)
    return value


def _check_param_range(key: str, value: int | float, spec: dict[str, Any]) -> None:
    minimum, maximum = spec.get("min"), spec.get("max")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        if value < minimum:
            raise RunSubmissionError(
                f"Parameter {key!r} must be at least {minimum:g}; got {value:g}."
            )
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        if value > maximum:
            raise RunSubmissionError(
                f"Parameter {key!r} must be at most {maximum:g}; got {value:g}."
            )


def _symbol_for(universe: list[str]) -> str:
    """The run row's single symbol field, derived from a strategy's universe.

    The client's row shape has one symbol and these strategies trade baskets,
    so a multi-ticker run is labelled ``"MULTI"`` and the real list stays on
    the strategy. Flagged to the frontend session as a known wart; a
    ``symbols: string[]`` field is the eventual fix.
    """
    tickers = [str(ticker).strip() for ticker in universe if str(ticker).strip()]
    return tickers[0] if len(tickers) == 1 else "MULTI"


async def submit_backtest_run(
    request: BacktestRunRequest, *, owner_id: uuid.UUID | None = None
) -> BacktestSummary:
    """Validate a submission, register a transient job, and dispatch it.

    No run or report is inserted here. The completion callback saves only a
    successful report; dispatch failure is returned immediately to the caller.
    Raises :class:`RunSubmissionError` for anything the student can fix.
    """
    logger.info(
        "SUBMIT | Backtest request received; strategy=%r window=%s..%s capital=%s mode=%s",
        request.strategy_key, request.start_date, request.end_date, request.initial_capital, request.mode,
    )
    name = _validated_name(request.name)
    strategy = await _load_runnable_strategy(request.strategy_key)
    start_date, end_date = _validated_window(request)
    initial_capital = _validated_capital(request.initial_capital)
    mode = _validated_mode(request.mode)
    try:
        strategy_params, controls, universe = split_controls(
            request.params, strategy.universe, mode
        )
    except ValueError as exc:
        raise RunSubmissionError(str(exc)) from None
    params = _validated_params(strategy, strategy_params)
    _validated_intraday_size(controls, universe, start_date, end_date)
    logger.info("SUBMIT | Settings validated; strategy=%s tickers=%s slippage_bps=%s commission_per_share=%s", strategy.key, universe, controls.get("slippageBps", 0), controls.get("commissionPerShare", 0))
    # Check the selected universe, not the registry's defaults.
    await _validated_coverage(universe, start_date, end_date)

    summary = await create_backtest_run(
        name=name,
        strategy_key=strategy.key,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        symbol=_symbol_for(universe),
        engine_version=ENGINE_VERSION,
        # The overlay the worker hands the engine, plus the reserved mode key
        # it pops back off first — there is no mode column to put it in.
        params={**params, **controls, "universe": universe, MODE_KEY: mode},
        owner_id=owner_id,
    )
    logger.info("QUEUED | Transient job registered; run=%s strategy=%s; dispatching to worker", summary.id, strategy.key)
    return await _dispatch(summary)


async def _dispatch(summary: BacktestSummary) -> BacktestSummary:
    """Hand an in-memory job to the worker pool; failed dispatch saves no report."""
    from src.workers.job_manager import get_job_manager
    await asyncio.to_thread(get_job_manager().submit, summary.id)
    return summary
