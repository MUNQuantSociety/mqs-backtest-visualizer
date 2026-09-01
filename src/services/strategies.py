"""Strategy catalogue business logic.

Sits between the routes and the repository: opens the session, turns ORM rows
into the Pydantic models the frontend parses, and owns the one translation the
database and the client disagree about — the status vocabulary.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal

from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import strategies as strategies_repo
from src.repositories.strategies import StrategyRow
from src.schemas.strategies import (
    CompatibilityIssue,
    CompatibilityStatus,
    ParameterSpec,
    Strategy,
    StrategyCheckRequest,
    StrategyCheckResult,
    StrategyListResponse,
    StrategyStatus,
    StrategySubmission,
    StrategySubmissionResult,
    StrategyTemplate,
)
from src.services import strategy_validation
from src.services.strategy_validation import template

# The registry tracks four states; the client's Zod enum knows three. Both
# in-flight states collapse to ``draft`` — the submission message is what tells
# a student whether validation is still running or has failed.
_STATUS_TO_CLIENT = {
    "active": StrategyStatus.ACTIVE,
    "validating": StrategyStatus.DRAFT,
    "failed_validation": StrategyStatus.DRAFT,
    "archived": StrategyStatus.ARCHIVED,
}

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

logger = logging.getLogger(__name__)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment is not None else None


def _float(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _class_name(class_path: str | None, key: str) -> str:
    """The class the engine instantiates, which is what the client displays.

    Uploaded strategies have no import path until their validation run loads
    them, so the key stands in — the field is never allowed to be empty.
    """
    if class_path:
        return class_path.rsplit(".", 1)[-1]
    return key


def _parameter_specs(raw: list[dict] | None) -> list[ParameterSpec]:
    """Validate the stored specs on the way out.

    They are JSONB written by the seed script, so they are trusted — but
    validating here means a malformed spec fails in one obvious place instead
    of rendering a broken form control in the browser.
    """
    return [ParameterSpec.model_validate(spec) for spec in (raw or [])]


def to_schema(row: StrategyRow) -> Strategy:
    """ORM row plus aggregates → the frontend's ``Strategy``."""
    strategy = row.strategy
    return Strategy(
        id=strategy.key,
        name=strategy.name,
        class_name=_class_name(strategy.class_path, strategy.key),
        description=strategy.description or "",
        status=_STATUS_TO_CLIENT.get(strategy.status, StrategyStatus.DRAFT),
        tags=list(strategy.tags or []),
        parameters=_parameter_specs(strategy.param_specs),
        universe=list(strategy.universe or []),
        run_count=row.run_count,
        best_sharpe=_float(row.best_sharpe),
        best_return=_float(row.best_return),
        last_run_at=_iso(row.last_run_at),
        validation_state=strategy.status,
        validation_run_id=(
            str(strategy.validation_run_id) if strategy.validation_run_id else None
        ),
    )


def _generate_key(name: str) -> str:
    """A stable, readable, collision-proof registry key for an upload.

    The slug is for humans reading URLs and log lines; the uuid suffix is what
    actually guarantees uniqueness, so two students uploading "Momentum" never
    race for the same row.
    """
    slug = _SLUG_PATTERN.sub("-", name.strip().lower()).strip("-")[:40] or "strategy"
    return f"user-{slug}-{uuid.uuid4().hex[:8]}"


async def list_strategies(include_disabled: bool = False) -> StrategyListResponse:
    """The catalogue, with per-strategy run aggregates computed in SQL."""
    await ensure_schema()
    async with session_scope() as session:
        rows = await strategies_repo.list_strategies(
            session, include_disabled=include_disabled
        )
        items = [to_schema(row) for row in rows]
    return StrategyListResponse(items=items, total=len(items))


async def get_strategy(key: str) -> Strategy | None:
    """One strategy by key, including ones the catalogue hides.

    This is the endpoint behind "is my upload done yet?": a validating or
    failed upload is disabled and therefore absent from the list, so a client
    that only has the list has no way to watch it. None when the key is unknown.
    """
    await ensure_schema()
    async with session_scope() as session:
        row = await strategies_repo.get_strategy_row(session, key)
    return to_schema(row) if row is not None else None


def strategy_template() -> StrategyTemplate:
    """The starter source, straight from the module the check tests against."""
    return StrategyTemplate(
        filename=template.STARTER_FILENAME, source=template.STARTER_SOURCE
    )


def check_strategy(request: StrategyCheckRequest) -> StrategyCheckResult:
    """Answer whether a file would run here, without creating anything.

    Synchronous and side-effect free on purpose: it reads the source with
    ``ast`` and touches no database, no store and no worker, so the editor can
    call it as often as a student presses the button. Nothing is stored, so a
    failed check leaves no trace and a passing one still has to be submitted.

    A pass is not a promise that the strategy works, only that it can be
    loaded and has the shape the engine drives. The proof is the validation
    backtest that :func:`submit_strategy` queues.
    """
    report = strategy_validation.check_compatibility(request.source)

    issues = [
        CompatibilityIssue(line=issue.line, message=issue.message)
        for issue in report.issues
    ]
    warnings = [
        CompatibilityIssue(line=warning.line, message=warning.message)
        for warning in report.warnings
    ]

    return StrategyCheckResult(
        status=(
            CompatibilityStatus.COMPATIBLE
            if report.compatible
            else CompatibilityStatus.INCOMPATIBLE
        ),
        ok=report.compatible,
        class_name=report.class_name,
        issues=issues,
        warnings=warnings,
        message=_check_message(report.compatible, report.class_name, issues, warnings),
    )


def _check_message(
    compatible: bool,
    class_name: str | None,
    issues: list[CompatibilityIssue],
    warnings: list[CompatibilityIssue],
) -> str:
    """The one sentence shown beside the verdict.

    The issues are listed in full underneath it, so this counts rather than
    repeats them, and it says what a pass does *not* mean, because "compatible"
    read as "this works" is the misunderstanding worth heading off.
    """
    if not compatible:
        count = len(issues)
        return (
            f"{count} problem{'' if count == 1 else 's'} to fix before this can "
            "run here."
        )

    subject = class_name or "This strategy"
    tail = (
        f" {len(warnings)} warning{'' if len(warnings) == 1 else 's'} worth reading."
        if warnings
        else ""
    )
    return (
        f"{subject} is compatible with the engine. Submitting it starts the "
        f"validation backtest that proves it runs.{tail}"
    )


async def submit_strategy(submission: StrategySubmission) -> StrategySubmissionResult:
    """Store an upload and start the backtest that proves it works.

    Four steps, in this order for a reason. The source is scanned first, so a
    rejected upload leaves nothing behind at all. It is then written to the
    strategy store, because the worker loads uploads from there and from
    nowhere else — a registry row pointing at no stored source is a strategy
    that can never run. Only then is the row inserted, disabled and invisible
    to the catalogue. Last, a normal backtest is queued against it with
    ``purpose='validation'``: same pipeline, same progress, same error
    reporting, and the worker flips this row to ``active`` when it passes.

    The response goes back immediately — validation takes as long as a backtest
    takes — with ``status="draft"``, which is what the client's enum calls
    everything that is not active. The message is where the real state lives.

    Raises :class:`~src.services.strategy_validation.StrategyValidationError`
    for source the student has to fix; the route turns that into a 422.
    """
    scan = strategy_validation.scan_source(submission.source)

    await ensure_schema()
    # One-shot catch-up for rows written before the store existed. It finds
    # nothing on every call after the first, and doing it here means the
    # migration needs no startup hook of its own.
    await strategy_validation.migrate_staged_sources()

    key = _generate_key(submission.name)
    config = strategy_validation.build_config(key)
    storage_key = strategy_validation.store_strategy_source(
        key, submission.source, config
    )

    async with session_scope() as session:
        await strategies_repo.create_strategy(
            session,
            key=key,
            name=submission.name,
            description=submission.description or "",
            kind="user",
            status="validating",
            # Never surfaces in the catalogue until a validation run passes.
            enabled=False,
            tags=["user"],
            universe=list(config["TICKERS"]),
            param_specs=strategy_validation.parameter_specs(),
            # An upload has no import path: the worker materializes this key
            # and imports the file it finds there.
            storage_key=storage_key,
            class_path=None,
        )

    message, run_id = await _begin_validation(
        key, submission.name, config, scan.class_name
    )
    return StrategySubmissionResult(
        id=key,
        name=submission.name,
        status=StrategyStatus.DRAFT,
        message=message,
        validation_run_id=run_id,
    )


async def _begin_validation(
    key: str, name: str, config: dict, class_name: str
) -> tuple[str, str | None]:
    """Queue the validation run; return the student-facing message and its id.

    A failure to *start* the run is not a failure of the upload, but it must
    not read as "still validating" either: the strategy is parked in
    ``failed_validation`` and the message says the run never started, so the
    student re-uploads instead of waiting for a result that is not coming.

    The run id is written onto the strategy row here, at submit time. The
    worker also writes it when the run finishes, but a client polling
    ``GET /strategies/{key}`` *during* validation needs it now — otherwise
    the only place it exists is inside this sentence.
    """
    try:
        summary = await strategy_validation.start_validation(
            strategy_key_value=key,
            strategy_name=name,
            tickers=list(config["TICKERS"]),
        )
    except Exception as exc:
        logger.exception("Validation run for strategy %s could not be started", key)
        await strategy_validation.mark_validation_unstarted(key, str(exc))
        return (
            f"Saved {class_name}, but its validation backtest could not be "
            f"started ({exc}). Try uploading it again."
        ), None

    async with session_scope() as session:
        await strategies_repo.set_validation_state(
            session,
            key,
            status="validating",
            enabled=False,
            validation_run_id=uuid.UUID(summary.id),
        )

    return (
        f"Validation backtest started for {class_name} — the strategy "
        f"activates when it passes. Follow run {summary.id} for progress."
    ), summary.id


async def delete_strategy(key: str) -> bool:
    """Remove a registry row and any source stored for it.

    The store is emptied after the row is gone, not before: an orphaned object
    in the store is invisible and harmless, while a row pointing at source that
    has been deleted is a strategy that fails at run time for no stated reason.
    """
    await ensure_schema()
    async with session_scope() as session:
        removed = await strategies_repo.delete_strategy(session, key)

    if removed:
        strategy_validation.discard_stored_source(key)
    return removed
