"""Strategy catalogue endpoints.

Backed by the ``app.strategies`` registry through
``src/services/strategies.py``; the seed script populates the built-ins.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError

from src.schemas.strategies import (
    MAX_SOURCE_BYTES,
    Strategy,
    StrategyCheckRequest,
    StrategyCheckResult,
    StrategyListResponse,
    StrategySubmission,
    StrategySubmissionResult,
    StrategyTemplate,
)
from src.services import strategies as strategies_service
from src.services.strategy_validation import StrategyValidationError

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("", response_model=StrategyListResponse)
async def list_strategies() -> StrategyListResponse:
    """Every enabled strategy, with its run aggregates computed in SQL.

    Disabled rows are hidden: a strategy is disabled either because it is a
    pipeline test harness or because an upload has not passed validation, and
    neither is something to offer a student.
    """
    return await strategies_service.list_strategies()


@router.post(
    "", response_model=StrategySubmissionResult, status_code=status.HTTP_201_CREATED
)
async def submit_strategy(submission: StrategySubmission) -> StrategySubmissionResult:
    """Accept strategy source, store it, and start its validation backtest.

    The submitted source is untrusted user code, and validating it means
    **executing** it in a worker process that holds admin database credentials.
    Two cheap guardrails run before anything is stored: a scan that refuses
    imports outside a small allowlist and the obvious escape hatches, and a
    check that the file defines exactly one ``BasePortfolio`` subclass. Both
    are speed bumps against accidents, **not** a sandbox — see the security
    note in ``src/services/strategy_validation.py``. Real isolation is required
    before this endpoint is exposed beyond the club.

    Responds immediately with ``status="draft"``: the validation run has only
    just been queued, and it takes as long as a backtest takes. The strategy
    turns ``active`` on its own when the run passes.
    """
    size = len(submission.source.encode("utf-8"))
    if size > MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Strategy source is {size} bytes; the limit is {MAX_SOURCE_BYTES}.",
        )

    try:
        return await strategies_service.submit_strategy(submission)
    except StrategyValidationError as exc:
        # ``detail`` is a plain string, not FastAPI's list of error objects:
        # the client shows it verbatim in the editor's error slot.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get("/template", response_model=StrategyTemplate)
async def get_template() -> StrategyTemplate:
    """Starter source for the editor.

    Served so the contract the editor teaches cannot drift from the engine that
    runs it. A test asserts this passes the compatibility check below.
    """
    return strategies_service.strategy_template()


@router.post("/check", response_model=StrategyCheckResult)
async def check_strategy(request: StrategyCheckRequest) -> StrategyCheckResult:
    """Say whether this source would run here. Always 200 when the check ran.

    The editor calls this before submitting so a student finds out about a
    banned import or a missing ``OnData`` in a millisecond, rather than minutes
    later when the validation backtest reports it.

    **Incompatible source still answers 200.** The request was well formed and
    the check completed; the verdict lives in the body, where ``ok`` is false
    and ``issues`` lists every problem with its line. A 4xx would say the
    request was wrong, and would flatten that list into one ``detail`` string.
    The two real failures keep their codes: source over the size limit is a
    413, and a malformed body is FastAPI's own 422.

    Nothing is stored and nothing is executed: the source is read with ``ast``.
    A pass therefore means "this can be loaded and has the right shape", not
    "this works"; ``POST /strategies`` is what queues the run that proves it.
    """
    size = len(request.source.encode("utf-8"))
    if size > MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Strategy source is {size} bytes; the limit is {MAX_SOURCE_BYTES}.",
        )

    return strategies_service.check_strategy(request)


# ---------------------------------------------------------------------------
# File uploads. Same pipeline as the JSON endpoints above; only the transport
# differs. The frontend normally reads a chosen file in the browser and sends
# its text, so these exist for clients that hold an actual file — a script,
# a CLI, a future drag-and-drop that streams instead of reading.
# ---------------------------------------------------------------------------

_SOURCE_SUFFIX = ".py"


async def _read_source_file(upload: UploadFile) -> tuple[str, str]:
    """Extract UTF-8 source from an uploaded ``.py`` file, or say why not.

    Every refusal is a 422 with one plain sentence in ``detail``, because that
    is the only error shape the client renders verbatim. The size check is a
    413 to match the JSON endpoints, which reject the same limit the same way.
    """
    filename = (upload.filename or "").strip()
    if not filename.lower().endswith(_SOURCE_SUFFIX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Expected a Python file ending in {_SOURCE_SUFFIX}, got "
                f"{filename or 'a file with no name'!r}."
            ),
        )

    # Read one byte past the limit rather than the whole file: a stray 2 GB
    # upload is refused after 256 KB, not after it has been buffered.
    raw = await upload.read(MAX_SOURCE_BYTES + 1)
    await upload.close()
    if len(raw) > MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{filename} is over the {MAX_SOURCE_BYTES} byte limit.",
        )
    if not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{filename} is empty.",
        )

    try:
        return raw.decode("utf-8"), filename
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{filename} is not UTF-8 text (byte {exc.start}).",
        ) from exc


@router.post("/upload/check", response_model=StrategyCheckResult)
async def check_strategy_file(file: UploadFile = File(...)) -> StrategyCheckResult:
    """``POST /strategies/check`` for a file instead of a JSON body.

    Identical verdict semantics: the check ran, so it is a 200 even when the
    answer is "no". Nothing is stored and nothing is executed.
    """
    source, filename = await _read_source_file(file)
    return strategies_service.check_strategy(
        StrategyCheckRequest(source=source, filename=filename)
    )


@router.post(
    "/upload",
    response_model=StrategySubmissionResult,
    status_code=status.HTTP_201_CREATED,
)
async def submit_strategy_file(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
) -> StrategySubmissionResult:
    """``POST /strategies`` for a multipart file instead of a JSON body.

    The file is read, decoded and handed to the same submission path — the
    same scan, the same store, the same validation backtest. ``name`` and
    ``description`` travel as form fields beside it. Same responses: 201 with
    ``validationRunId`` to poll, 422 for anything the student has to fix, 413
    over the size limit.
    """
    source, filename = await _read_source_file(file)
    try:
        submission = StrategySubmission(
            name=name, description=description, source=source, filename=filename
        )
    except ValidationError as exc:
        # One sentence, not Pydantic's list: the first problem is enough to act on.
        first = exc.errors()[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ) from exc

    try:
        return await strategies_service.submit_strategy(submission)
    except StrategyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


# Declared last on purpose: a path parameter would otherwise swallow
# ``/template``, ``/check`` and ``/upload`` above it.
@router.get("/{key}", response_model=Strategy)
async def get_strategy(key: str) -> Strategy:
    """One strategy, **including the ones the catalogue hides**.

    ``GET /strategies`` shows only enabled rows, so an upload that is still
    validating — or one that failed — is invisible there. This is how a
    client watches a submission: ``validationState`` carries the real
    lifecycle (``validating`` / ``active`` / ``failed_validation``) and
    ``validationRunId`` is the backtest to open for progress or the failure
    reason. ``status`` stays within the client's enum (``draft`` until active).
    """
    strategy = await strategies_service.get_strategy(key)
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No strategy with id {key!r}.",
        )
    return strategy
