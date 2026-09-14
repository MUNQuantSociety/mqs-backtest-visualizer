"""Strategy catalogue business logic.

Sits between the routes and the repository: opens the session, turns ORM rows
into the Pydantic models the frontend parses, and owns the one translation the
database and the client disagree about — the status vocabulary.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.repositories import strategies as strategies_repo
from src.repositories.strategies import StrategyRow
from src.schemas.strategies import (
    MAX_SOURCE_BYTES,
    CompatibilityIssue,
    CompatibilityStatus,
    ParameterSpec,
    Strategy,
    IndicatorSpec,
    StrategyDraftRequest,
    StrategyDraftSubmission,
    StrategyCheckRequest,
    StrategyCheckResult,
    StrategyListResponse,
    StrategyStatus,
    StrategySubmission,
    StrategySubmissionResult,
    StrategySource,
    StrategyTemplate,
)
from src.services import strategy_validation
from src.services.strategy_validation import authoring
from src.services.strategy_validation.scanning import declared_indicators
from src.integrations.strategy_store import get_strategy_store
from src.services.strategy_availability import package_available
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


@lru_cache(maxsize=64)
def _builtin_indicators(class_path: str) -> tuple[str, ...]:
    """A built-in's declared indicators, read from its vendored file.

    Cached: the engine's files do not change while the process runs, and this
    would otherwise re-read and re-parse them on every catalogue request.
    """
    module = class_path.rsplit(".", 1)[0]
    path = Path(*module.split(".")).with_suffix(".py")
    try:
        return tuple(declared_indicators(path.read_text(encoding="utf-8")))
    except OSError:
        # A vendored file that moved is not worth failing a list request over.
        logger.warning("CATALOGUE | Could not read %s for indicators", path)
        return ()


def _row_indicators(strategy) -> list[str]:
    """What this strategy registers, from whichever source describes it.

    A fragment-authored strategy carries its spec on the row, so nothing has to
    be parsed. A built-in points at a vendored class. An uploaded whole file has
    neither, and reading it would mean a store round trip per row on every
    catalogue request — too expensive for a label, so it reports nothing.
    """
    authored = (strategy.authoring or {}).get("indicators")
    if authored:
        return sorted({spec["indicator"] for spec in authored})
    if strategy.class_path:
        return list(_builtin_indicators(strategy.class_path))
    return []


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
        indicators=_row_indicators(strategy),
        validation_state=strategy.status,
        validation_run_id=(
            str(getattr(strategy, "validation_job_id", None) or strategy.validation_run_id)
            if (getattr(strategy, "validation_job_id", None) or strategy.validation_run_id) else None
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
    """Registered strategies and aggregates, backed by complete S3 packages.

    The registry remains the validation/ownership catalogue: arbitrary S3
    objects and incomplete uploads must never become executable strategies.
    Local mode retains the vendored built-ins for offline development.
    """
    started = time.perf_counter()
    logger.info("CATALOGUE | GET strategies received; storage=%s", settings.strategy_store_backend)
    await ensure_schema()
    async with session_scope() as session:
        rows = await strategies_repo.list_strategies(
            session, include_disabled=include_disabled
        )
    # Availability is checked for every backend, but what "missing" means
    # differs. Under S3 a row with no published package is not runnable at all,
    # including a built-in that was never uploaded — that is the rule this
    # already had. Locally the built-ins are vendored in the image and load
    # without a store, so only rows that claim a package are checked.
    #
    # Local is checked at all because "a local store cannot lose files" was
    # never true: it is a directory inside the container, and one
    # `docker compose up --build` without a volume empties it while every row
    # still claims a package. Offering a strategy the worker cannot load turns
    # a missing file into a stack trace minutes later, inside a run.
    on_s3 = settings.strategy_store_backend == "s3"
    limit = asyncio.Semaphore(4)

    async def available(row: StrategyRow) -> bool:
        if not row.strategy.storage_key:
            return not on_s3
        async with limit:
            return await package_available(row.strategy.storage_key)

    present = await asyncio.gather(*(available(row) for row in rows))
    missing = [row.strategy.key for row, exists in zip(rows, present) if not exists]
    if missing:
        logger.warning(
            "CATALOGUE | Hiding %d strategy(ies) with no loadable package: %s",
            len(missing),
            missing,
        )
    rows = [row for row, exists in zip(rows, present) if exists]
    items = [to_schema(row) for row in rows]
    logger.info("CATALOGUE | Returning %d strategies: %s; elapsed_ms=%.0f", len(items), [item.id for item in items], (time.perf_counter() - started) * 1000)
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
    """The starter, as a whole file and as a fragment.

    Both halves come from the same text: ``body`` and ``indicators`` are read
    back out of ``source``, so the full-file editor and the fragment editor
    cannot be taught different contracts.
    """
    return StrategyTemplate(
        filename=template.STARTER_FILENAME,
        source=template.STARTER_SOURCE,
        body=template.STARTER_BODY,
        indicators=[
            IndicatorSpec(attribute=attribute, indicator=indicator, params=params)
            for attribute, indicator, params in template.STARTER_INDICATORS
        ],
        state=dict(template.STARTER_STATE),
    )


def _draft(request: StrategyDraftRequest) -> authoring.StrategyDraft:
    return authoring.StrategyDraft(
        body=request.body,
        state=dict(request.state or {}),
        indicators=tuple(
            authoring.IndicatorSpec(
                attribute=spec.attribute, indicator=spec.indicator, params=spec.params
            )
            for spec in request.indicators
        ),
    )


def check_draft(request: StrategyDraftRequest) -> StrategyCheckResult:
    """Check a fragment, reporting every line as the member's own.

    Same verdict semantics as :func:`check_strategy` — incompatible is a
    successful check — plus the assembled file, so the editor can show what
    will actually run.
    """
    draft = _draft(request)
    assembled = authoring.assemble(draft)
    report = authoring.check_draft(draft)

    return StrategyCheckResult(
        status=(
            CompatibilityStatus.COMPATIBLE
            if report.compatible
            else CompatibilityStatus.INCOMPATIBLE
        ),
        ok=report.compatible,
        class_name=report.class_name,
        issues=[
            CompatibilityIssue(line=issue.line, message=issue.message)
            for issue in report.issues
        ],
        warnings=[
            CompatibilityIssue(line=warning.line, message=warning.message)
            for warning in report.warnings
        ],
        message=(
            f"{report.class_name} is compatible with the engine. Submitting it "
            "starts the validation backtest that proves it runs."
            if report.compatible
            else f"{len(report.issues)} problem"
            f"{'' if len(report.issues) == 1 else 's'} to fix before this can run here."
        ),
        assembled_source=assembled.source,
        body_offset=assembled.body_offset,
    )


async def submit_draft(
    submission: StrategyDraftSubmission, *, owner_id: uuid.UUID | None = None
) -> StrategySubmissionResult:
    """Assemble a fragment and submit it exactly as an uploaded file.

    The whole point is the delegation: once assembled there is nothing special
    about a draft, so it goes through :func:`submit_strategy` — same scan, same
    store, same registry row, same validation backtest. Nothing about the
    upload path is duplicated or forked here.
    """
    # assemble_checked, not assemble: the submit path has to apply the same
    # structural assertions the check endpoint does, or a draft refused by one
    # is stored by the other.
    assembled = authoring.assemble_checked(_draft(submission))

    # The pre-assembly body limit does not bound the file: indicators and state
    # add to it. `POST /strategies` enforces this at the route, and this path
    # goes straight to the service, so it enforces it here.
    size = len(assembled.source.encode("utf-8"))
    if size > MAX_SOURCE_BYTES:
        raise strategy_validation.StrategyValidationError(
            f"The assembled strategy is {size} bytes; the limit is {MAX_SOURCE_BYTES}."
        )
    logger.info(
        "UPLOAD | Assembled a draft; name=%r body_lines=%d indicators=%d",
        submission.name,
        assembled.body_lines,
        len(submission.indicators),
    )
    return await submit_strategy(
        StrategySubmission(
            name=submission.name,
            description=submission.description,
            source=assembled.source,
            filename=submission.filename,
        ),
        # Recorded so the editor can reopen this as the fragment it was, rather
        # than as the assembled file. Without it, editing a draft would hand
        # back generated boilerplate the member never wrote.
        authoring={
            "body": submission.body,
            "indicators": [spec.model_dump() for spec in submission.indicators],
            "state": dict(submission.state),
        },
        owner_id=owner_id,
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


async def submit_strategy(
    submission: StrategySubmission,
    *,
    authoring: dict | None = None,
    owner_id: uuid.UUID | None = None,
) -> StrategySubmissionResult:
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
    logger.info("UPLOAD | Checking strategy source; name=%r bytes=%d", submission.name, len(submission.source.encode("utf-8")))
    scan = strategy_validation.scan_source(submission.source)
    logger.info("UPLOAD | Compatibility passed; class=%s", scan.class_name)

    await ensure_schema()
    # One-shot catch-up for rows written before the store existed. It finds
    # nothing on every call after the first, and doing it here means the
    # migration needs no startup hook of its own.
    await strategy_validation.migrate_staged_sources()

    key = _generate_key(submission.name)
    config = strategy_validation.build_config(key)
    storage_key = await asyncio.to_thread(
        strategy_validation.store_strategy_source, key, submission.source, config
    )
    logger.info("UPLOAD | Source/config stored; strategy=%s storage=%s key=%s", key, settings.strategy_store_backend, storage_key)

    try:
        async with session_scope() as session:
            await strategies_repo.create_strategy(
                session,
                key=key,
                name=submission.name,
                description=submission.description or "",
                kind="user",
                status="validating",
                # Not selectable until its real validation backtest passes.
                enabled=False,
                tags=["user"],
                universe=list(config["TICKERS"]),
                param_specs=strategy_validation.parameter_specs(),
                storage_key=storage_key,
                authoring=authoring,
                class_path=None,
            )
    except Exception:
        await _discard_unregistered_source(key)
        raise

    logger.info("UPLOAD | Draft registered; strategy=%s; queueing validation", key)
    message, run_id = await _begin_validation(
        key, submission.name, config, scan.class_name, owner_id=owner_id
    )
    return StrategySubmissionResult(
        id=key,
        name=submission.name,
        status=StrategyStatus.DRAFT,
        message=message,
        validation_run_id=run_id,
    )


async def _begin_validation(
    key: str, name: str, config: dict, class_name: str, *, owner_id: uuid.UUID | None = None
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
            owner_id=owner_id,
        )
    except Exception as exc:
        logger.exception("Validation run for strategy %s could not be started", key)
        await strategy_validation.mark_validation_unstarted(key, str(exc))
        return (
            f"Saved {class_name}, but its validation backtest could not be "
            f"started ({exc}). Try uploading it again."
        ), None

    async with session_scope() as session:
        await strategies_repo.attach_validation_run(session, key, uuid.UUID(summary.id))

    return (
        f"Validation backtest started for {class_name} — the strategy "
        f"activates when it passes. Follow run {summary.id} for progress."
    ), summary.id


async def _discard_unregistered_source(key: str) -> None:
    """Clean a fresh failed upload only if its registry transaction did not commit.

    An uncertain commit must not leave a live registry entry pointing at deleted
    source. If the verification read also fails, retain the package for recovery.
    """
    try:
        async with session_scope() as session:
            registered = await strategies_repo.get_strategy(session, key)
        if registered is None:
            await asyncio.to_thread(strategy_validation.discard_stored_source, key)
    except Exception:
        logger.exception(
            "Retaining source for %s after an uncertain registry write", key
        )


async def get_strategy_source(key: str) -> StrategySource | None:
    """The Python this strategy was registered with, read back from the store.

    This is what makes a saved strategy editable. Source went into the store on
    upload and nothing could read it back out, so the editor could only ever
    start from the template — correcting one typo meant retyping the file.

    None when the key is unknown, or when the row has no stored package: the
    built-ins that ship with the engine were never uploaded. Read as text and
    never imported, because answering a GET must not execute anything.
    """
    await ensure_schema()
    async with session_scope() as session:
        row = await strategies_repo.get_strategy_row(session, key)

    if row is None or not row.strategy.storage_key:
        return None

    storage_key = row.strategy.storage_key
    try:
        source = await asyncio.to_thread(_read_stored_source, storage_key)
    except KeyError:
        # The row outlived its package. Reported as "no source" rather than a
        # 500: there is nothing the caller can do about it, and the registry
        # entry itself is still real.
        logger.warning("SOURCE | Package missing; strategy=%s key=%s", key, storage_key)
        return None

    # A fragment-authored strategy reopens as its fragment; an uploaded one
    # reopens as the file. `authoring` being NULL is what distinguishes them.
    authored = row.strategy.authoring or {}
    return StrategySource(
        filename="strategy.py",
        source=source,
        body=authored.get("body"),
        indicators=(
            [IndicatorSpec(**spec) for spec in authored["indicators"]]
            if authored.get("indicators") is not None
            else None
        ),
        state=authored.get("state"),
    )


def _read_stored_source(storage_key: str) -> str:
    return get_strategy_store().get(storage_key, "strategy.py")


class StrategyInUse(RuntimeError):
    """A strategy has backtests recorded against it and cannot be deleted.

    Runs hold a ``RESTRICT`` foreign key to the strategy, so the database
    refuses the delete at commit. Translated here so routes never have to know
    what an ``IntegrityError`` is.
    """


async def delete_strategy(key: str) -> bool:
    """Remove a registry row and any source stored for it.

    The store is emptied after the row is gone, not before: an orphaned object
    in the store is invisible and harmless, while a row pointing at source that
    has been deleted is a strategy that fails at run time for no stated reason.

    Raises :class:`StrategyInUse` when runs still reference the strategy.
    """
    await ensure_schema()
    try:
        async with session_scope() as session:
            removed = await strategies_repo.delete_strategy(session, key)
    except IntegrityError as exc:
        raise StrategyInUse(key) from exc

    if removed:
        await asyncio.to_thread(strategy_validation.discard_stored_source, key)
    return removed
