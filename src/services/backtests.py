"""Backtest business logic — ORM rows in, frontend contract out, runs queued.

Reading is the smaller half. The other half is ``submit_backtest_run``, which
is where a student's Run Backtest click becomes a row and a queued job: it
validates the submission against the strategy registry, inserts the run, and
hands the id to the worker pool.

The interesting decision on the read side is how a run that has not finished
serialises. The client's Zod schema declares ``finalEquity``, ``totalReturn``,
``sharpe`` and ``maxDrawdown`` as plain numbers, but a queued run has none of
them yet. Rather than break the contract (or make the client handle nulls it
did not ask for), an unfinished run reports zeros. The status field is what
tells the UI whether those numbers mean anything.
"""

from __future__ import annotations

import logging
import math
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

# A constant, not the engine: ``engine/__init__.py`` imports nothing, so
# stamping a run with the code that will execute it costs no pandas import.
from engine import ENGINE_VERSION
from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.models import BacktestRun, RunEquityPoint, RunMetrics, RunTrade
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.repositories.runs import TERMINAL_STATUSES, RunFilters, RunListRow
from src.services import market_data as market_data_service
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

# Execution modes the engine offers. ``fast`` is the vectorised path and not
# every strategy implements it; the engine rejects it per run with a message
# naming the strategy, which is a better answer than importing the class here
# just to refuse the request a second earlier.
RUN_MODES = ("event", "fast")

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

    A terminal run is removed, and so is a queued one no worker has claimed
    yet. Only a *running* run survives the request as a cancellation, because
    the worker owns that row and needs it to record why it stopped. Both answer
    204; the route needs the distinction only for its 404 case.
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
    )


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
    return BacktestDetail(
        **summary.model_dump(),
        metrics=_to_metrics(run.metrics),
        equity_curve=[_to_equity_point(point) for point in run.equity_points],
        trades=[_to_trade(run, trade) for trade in run.trades],
        parameters=dict(run.params or {}),
        progress_pct=run.progress_pct,
        error_message=run.error_message,
    )


async def list_backtests(
    *,
    search: str | None = None,
    status: BacktestStatus | None = None,
    strategy_id: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> BacktestListResponse:
    """One page of runs. Empty until the run pipeline writes its first row."""
    await ensure_schema()
    filters = RunFilters(
        search=search,
        status=status.value if status is not None else None,
        strategy_key=strategy_id,
    )
    async with session_scope() as session:
        rows, total = await runs_repo.list_runs(session, filters, page, page_size)
        items = [_to_summary(row) for row in rows]
    return BacktestListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


async def get_backtest(run_id: str) -> BacktestDetail | None:
    """One run in full, or None when the id is unknown or not a UUID."""
    parsed = runs_repo.parse_run_id(run_id)
    if parsed is None:
        return None

    await ensure_schema()
    async with session_scope() as session:
        row = await runs_repo.get_run(session, parsed)
        if row is None:
            return None
        return to_detail(row)


def _remove_artifacts(run_id: uuid.UUID) -> None:
    """Delete the engine's CSV output for a run. Missing is fine, failure is not fatal.

    Artifacts are derived data — the database row is the record — so a
    directory that will not delete (a file open in Excel, an OneDrive sync
    lock) must not turn a successful delete into a 500.
    """
    shutil.rmtree(settings.artifact_dir / str(run_id), ignore_errors=True)


async def delete_backtest(run_id: str) -> DeleteOutcome:
    """Delete a finished or unclaimed run; ask a running one to cancel."""
    parsed = runs_repo.parse_run_id(run_id)
    if parsed is None:
        return DeleteOutcome.NOT_FOUND

    await ensure_schema()
    async with session_scope() as session:
        row = await runs_repo.get_run(session, parsed)
        if row is None:
            return DeleteOutcome.NOT_FOUND

        if row.run.status in TERMINAL_STATUSES:
            await runs_repo.delete_run(session, parsed)
            _remove_artifacts(parsed)
            return DeleteOutcome.DELETED

        # A queued run is deleted outright rather than cancelled. Nothing has
        # claimed it, so nothing would ever act on the cancel flag or move it
        # to a terminal status — the row would sit in `queued` forever while
        # the client, which dropped it from its cache the moment it asked for
        # the delete, watches it reappear on the next list refetch. The
        # repository's predicate makes losing the race to a worker safe.
        if row.run.status == "queued" and await runs_repo.delete_unclaimed_run(
            session, parsed
        ):
            _remove_artifacts(parsed)
            return DeleteOutcome.DELETED

        await runs_repo.request_cancel(session, parsed)
        return DeleteOutcome.CANCEL_REQUESTED


async def create_backtest_run(
    *,
    name: str,
    strategy_key: str,
    start_date: date,
    end_date: date,
    initial_capital: float,
    symbol: str,
    engine_version: str,
    params: dict | None = None,
    purpose: str = "user",
    owner_id: uuid.UUID | None = None,
) -> BacktestSummary:
    """Insert a queued run and return the row the client can list immediately.

    Submitting it to the worker pool is the run-endpoint's job, not this one's:
    persisting the row and dispatching it are separate failures, and a dispatch
    that fails must still leave a run the student can see.
    """
    await ensure_schema()
    async with session_scope() as session:
        run = await runs_repo.create_run(
            session,
            name=name,
            strategy_key=strategy_key,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            symbol=symbol,
            engine_version=engine_version,
            params=params,
            purpose=purpose,
            owner_id=owner_id,
        )
        row = await runs_repo.get_run(session, run.id)
        return _to_summary(row)


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
        return _RunnableStrategy(
            key=strategy.key,
            universe=list(strategy.universe or []),
            param_specs=list(strategy.param_specs or []),
        )


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

    coverage = await market_data_service.coverage_for(universe)

    if coverage.missing:
        raise RunSubmissionError(
            f"There is no market data for {', '.join(coverage.missing)}, so this "
            "strategy cannot be backtested over any window."
        )
    if coverage.start is None or coverage.end is None:
        return

    if start.isoformat() < coverage.start or end.isoformat() > coverage.end:
        raise RunSubmissionError(
            f"There is only data from {coverage.start} to {coverage.end} for "
            f"{', '.join(universe)}. Pick a window inside that range."
        )


def _validated_capital(raw: float) -> float:
    """Capital has to be positive and finite — it divides every return."""
    capital = float(raw)
    if not math.isfinite(capital):
        raise RunSubmissionError(f"initialCapital must be a finite number; got {raw!r}.")
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


async def submit_backtest_run(request: BacktestRunRequest) -> BacktestSummary:
    """Validate a submission, queue the run, and return the row to show for it.

    Persisting and dispatching are two different failures and are handled
    separately on purpose. The row is inserted and committed first; only then
    is the job offered to the worker pool. If the pool refuses it — shut down,
    or broken by a worker that died — the run is marked ``failed`` with the
    reason rather than left ``queued`` forever, which is the one state a
    student cannot tell apart from a busy queue.

    Raises :class:`RunSubmissionError` for anything the student can fix.
    """
    name = _validated_name(request.name)
    strategy = await _load_runnable_strategy(request.strategy_key)
    start_date, end_date = _validated_window(request)
    # After the cheap checks and after the strategy is known, because it needs
    # both the universe and a database round trip per ticker.
    await _validated_coverage(list(strategy.universe or []), start_date, end_date)
    initial_capital = _validated_capital(request.initial_capital)
    mode = _validated_mode(request.mode)
    params = _validated_params(strategy, request.params)

    summary = await create_backtest_run(
        name=name,
        strategy_key=strategy.key,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        symbol=_symbol_for(strategy.universe),
        engine_version=ENGINE_VERSION,
        # The overlay the worker hands the engine, plus the reserved mode key
        # it pops back off first — there is no mode column to put it in.
        params={**params, MODE_KEY: mode},
    )
    return await _dispatch(summary)


async def _dispatch(summary: BacktestSummary) -> BacktestSummary:
    """Hand a queued run to the worker pool, or fail it with the reason.

    The job manager is imported here rather than at module scope so importing
    this service does not drag in the engine — and pandas, and numpy — through
    the worker module. The API process loads them anyway through its lifespan;
    a script or a test that only wants to read runs does not.
    """
    from src.workers.job_manager import get_job_manager

    try:
        get_job_manager().submit(summary.id)
    except Exception as exc:
        logger.error("Run %s could not be queued: %s", summary.id, exc)
        await _fail_undispatched(summary.id, f"Could not be queued to run: {exc}")
        # Reported as failed rather than queued: this response is what the
        # client inserts into its list cache, and a row claiming to be queued
        # when nothing will ever run it is worse than an error it can show.
        return summary.model_copy(update={"status": BacktestStatus.FAILED})
    return summary


async def _fail_undispatched(run_id: str, message: str) -> None:
    """Mark a run failed after its dispatch was refused. Best effort.

    If even this write fails the row is still there and still ``queued``, which
    the startup reconciler and the operator can both see. Raising instead would
    lose the response describing a run that does exist.
    """
    parsed = runs_repo.parse_run_id(run_id)
    if parsed is None:  # pragma: no cover - the id came from the row just created
        return
    try:
        async with session_scope() as session:
            await runs_repo.fail_unclaimed_run(session, parsed, message)
    except Exception:
        logger.exception(
            "Run %s could not be marked failed after a refused dispatch", run_id
        )
