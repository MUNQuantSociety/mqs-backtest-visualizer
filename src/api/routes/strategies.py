"""Strategy catalogue endpoints.

Backed by the ``app.strategies`` registry through
``src/services/strategies.py``; the seed script populates the built-ins.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from src.schemas.strategies import (
    MAX_SOURCE_BYTES,
    StrategyListResponse,
    StrategySubmission,
    StrategySubmissionResult,
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
