"""Backtest endpoints.

Query parameters are camelCase because the client sends its filter object
straight through as query params — ``strategyId``, not ``strategy_id``.

Everything here goes through ``src/services/backtests.py``, which owns the
session and the worker pool; this module deliberately knows nothing about
SQLAlchemy or the engine. ``POST /backtests`` is the endpoint the application
exists for — see :func:`create_backtest`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status

from src.schemas.backtests import (
    BacktestDetail,
    BacktestListResponse,
    BacktestRunRequest,
    BacktestStatus,
    BacktestSummary,
)
from src.services import backtests as backtests_service
from src.services.backtests import DeleteOutcome, RunSubmissionError

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("", response_model=BacktestListResponse)
async def list_backtests(
    search: str | None = Query(default=None),
    status_filter: BacktestStatus | None = Query(default=None, alias="status"),
    strategy_id: str | None = Query(default=None, alias="strategyId"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
) -> BacktestListResponse:
    """Runs, newest first. An empty list is a valid answer, not an error."""
    return await backtests_service.list_backtests(
        search=search,
        status=status_filter,
        strategy_id=strategy_id,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=BacktestSummary,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_backtest(submission: BacktestRunRequest) -> BacktestSummary:
    """Run a backtest. Answers **202** with the row, not the result.

    The engine takes minutes, so the response is the queued run itself: the
    client drops it straight into its list cache and polls
    ``GET /backtests/{id}`` (which carries ``progressPct``) until the status is
    terminal.

    A 202 does not promise the run will succeed — only that it exists and is
    the client's to watch. In the one case where the worker pool refuses the
    job outright, the payload comes back already marked ``failed`` with the
    reason on the row, because a run nothing will ever pick up must not look
    like a run waiting its turn.

    422 with a single-sentence ``detail`` for anything the student can fix: an
    unknown or disabled strategy, a malformed or backwards date range, a window
    past ``MAX_BACKTEST_WINDOW_DAYS``, capital of zero, or a parameter the
    strategy does not accept.
    """
    try:
        return await backtests_service.submit_backtest_run(submission)
    except RunSubmissionError as exc:
        # A string ``detail``, not FastAPI's list of error objects: the
        # client's error reader takes `detail` only when it is a string, and
        # shows "Request failed with status code 422" otherwise.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get("/{backtest_id}", response_model=BacktestDetail)
async def get_backtest(backtest_id: str) -> BacktestDetail:
    detail = await backtests_service.get_backtest(backtest_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    return detail


@router.delete("/{backtest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backtest(backtest_id: str) -> Response:
    """Delete a finished run, or cancel one that is still going.

    Two behaviours behind one verb, because that is what the UI's delete button
    means in both states:

    * ``completed``/``failed`` — the run, its metrics, its equity curve, its
      trades, and the engine's CSV artifact directory are all removed;
    * ``queued`` — no worker has claimed it, so the row is removed too. It is
      deleted under a ``status = 'queued'`` predicate, so a run claimed in the
      same instant is cancelled instead of vanishing under its worker;
    * ``running`` — the run cannot be deleted out from under the worker that
      owns it, so ``cancel_requested`` is set and this returns immediately. The
      worker notices within a second, unwinds the engine, and the run lands as
      ``failed`` with ``errorMessage = "Cancelled by user"``. The row stays;
      deleting it afterwards takes the terminal path above.

    Both answer 204. An unknown id is 404.
    """
    outcome = await backtests_service.delete_backtest(backtest_id)
    if outcome is DeleteOutcome.NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No backtest with id {backtest_id!r}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
