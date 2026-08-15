"""Strategy catalogue endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from src.schemas.strategies import (
    MAX_SOURCE_BYTES,
    StrategyListResponse,
    StrategyStatus,
    StrategySubmission,
    StrategySubmissionResult,
)
from src.services import sample_data

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("", response_model=StrategyListResponse)
async def list_strategies() -> StrategyListResponse:
    items = sample_data.list_strategies()
    return StrategyListResponse(items=items, total=len(items))


@router.post("", response_model=StrategySubmissionResult, status_code=status.HTTP_201_CREATED)
async def submit_strategy(submission: StrategySubmission) -> StrategySubmissionResult:
    """Accept strategy source and record it as a draft.

    The source is untrusted user code. It is stored and nothing more — no
    import, no compile, no execution. Running it needs the sandboxed worker
    pool (no network egress, no database credentials, CPU and wall-clock caps)
    described in the platform plan, which does not exist yet. Until then this
    endpoint returns ``draft`` and says plainly that nothing was validated.
    """
    size = len(submission.source.encode("utf-8"))
    if size > MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Strategy source is {size} bytes; the limit is {MAX_SOURCE_BYTES}.",
        )

    return StrategySubmissionResult(
        id=f"draft-{uuid.uuid4().hex[:12]}",
        name=submission.name,
        status=StrategyStatus.DRAFT,
        message="Saved as a draft. Validation and sandboxed execution are not built yet.",
    )
