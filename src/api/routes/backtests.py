"""Backtest endpoints.

Query parameters are camelCase because the client sends its filter object
straight through as query params — ``strategyId``, not ``strategy_id``.

Everything here goes through ``src/services/backtests.py``, which owns the
session and the worker pool; this module deliberately knows nothing about
database internals or the engine. ``POST /backtests`` is the endpoint the
application exists for — see :func:`create_backtest`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from src.services.backtests import FMPUnavailable
from src.api.dependencies.current_user import require_current_user
from src.schemas.backtests import (
    BacktestDetail,
    BacktestEquity,
    BacktestListResponse,
    BacktestRunRequest,
    BacktestStatus,
    BacktestSummary,
    LookbackPeriod,
)
from src.services import backtests as backtests_service
from src.services.backtests import DeleteOutcome, RunSubmissionError
from src.services.backtest_equity import window_equity
from src.services.report_exports import EXPORT_NAMES, export_report

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("", response_model=BacktestListResponse)
async def list_backtests(
    search: str | None = Query(default=None),
    status_filter: BacktestStatus | None = Query(default=None, alias="status"),
    strategy_id: str | None = Query(default=None, alias="strategyId"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
    owner_id: uuid.UUID = Depends(require_current_user),
) -> BacktestListResponse:
    """This user's saved successful reports, newest first."""
    return await backtests_service.list_backtests(
        owner_id=owner_id,
        search=search,
        status=status_filter,
        strategy_id=strategy_id,
        page=page,
        page_size=page_size,
    )


@router.get("/examples", response_model=BacktestListResponse)
async def list_example_backtests(
    search: str | None = Query(default=None),
    status_filter: BacktestStatus | None = Query(default=None, alias="status"),
    strategy_id: str | None = Query(default=None, alias="strategyId"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
    owner_id: uuid.UUID = Depends(require_current_user),
) -> BacktestListResponse:
    """This user's clearly labelled simulations, excluded from real history."""
    return await backtests_service.list_example_backtests(
        owner_id=owner_id, search=search, status=status_filter,
        strategy_id=strategy_id, page=page, page_size=page_size,
    )


@router.get("/active", response_model=list[BacktestSummary])
async def list_live_backtests(
    owner_id: uuid.UUID = Depends(require_current_user),
) -> list[BacktestSummary]:
    """This user's runs still queued or running, newest first.

    History holds saved reports only, so this is how a browser that did not
    submit a run can show it before it finishes. Declared before
    ``/{backtest_id}`` so ``active`` is never read as a run id.
    """
    return await backtests_service.list_live_backtests(owner_id=owner_id)


@router.post(
    "",
    response_model=BacktestSummary,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_backtest(
    submission: BacktestRunRequest,
    owner_id: uuid.UUID = Depends(require_current_user),
) -> BacktestSummary:
    """Accept a transient job. Only successful completion creates a saved report.

    The 202 body and detail polling retain the existing frontend contract.
    Live progress and errors are temporary; history contains saved reports only.
    """
    try:
        return await backtests_service.submit_backtest_run(submission, owner_id=owner_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except FMPUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except RunSubmissionError as exc:
        logging.getLogger(__name__).warning("REJECTED | Backtest request rejected; strategy=%r reason=%s", submission.strategy_key, str(exc))
        # A string ``detail``, not FastAPI's list of error objects: the
        # client's error reader takes `detail` only when it is a string, and
        # shows "Request failed with status code 422" otherwise.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get("/{backtest_id}", response_model=BacktestDetail)
async def get_backtest(backtest_id: str, owner_id: uuid.UUID = Depends(require_current_user)) -> BacktestDetail:
    detail = await backtests_service.get_backtest(backtest_id, owner_id=owner_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    return detail


@router.get("/{backtest_id}/equity", response_model=BacktestEquity)
async def get_backtest_equity(
    backtest_id: str,
    owner_id: uuid.UUID = Depends(require_current_user),
    period: LookbackPeriod = Query(),
    end_date: date = Query(alias="endDate", ge=date(6, 1, 1)),
) -> BacktestEquity:
    """Slice saved equity and benchmark observations; never run the engine.

    The dashboard passes the latest selected run's endDate to every strategy,
    so 1Y/2Y/5Y use the same calendar window. Max returns all saved observations
    through endDate. Available bounds explain short or missing history.
    """
    detail = await backtests_service.get_backtest(backtest_id, owner_id=owner_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No backtest with id {backtest_id!r}.")
    return window_equity(detail, period, end_date)


@router.delete("/{backtest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backtest(backtest_id: str, owner_id: uuid.UUID = Depends(require_current_user)) -> Response:
    """Delete an owned saved report, or cancel its transient execution."""
    outcome = await backtests_service.delete_backtest(backtest_id, owner_id=owner_id)
    if outcome is DeleteOutcome.NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{backtest_id}/exports/{filename}")
async def download_report(backtest_id: str, filename: str, owner_id: uuid.UUID = Depends(require_current_user)) -> Response:
    """Download one completed run as CSV or the exact detail JSON contract."""
    if filename not in EXPORT_NAMES:
        raise HTTPException(status_code=404, detail="Unknown report export.")
    detail = await backtests_service.get_backtest(backtest_id, owner_id=owner_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Backtest not found.")
    if detail.status is not BacktestStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Exports are available only after a successful backtest.")
    result = export_report(detail, filename)
    return Response(content=result.content, media_type=result.media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
