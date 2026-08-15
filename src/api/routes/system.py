"""System health and log tail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from src.schemas.system import LogTailResponse, SystemStatus
from src.services import sample_data

router = APIRouter(prefix="/live/system", tags=["live-system"])


@router.get("/status", response_model=SystemStatus)
async def get_status() -> SystemStatus:
    return sample_data.system_status()


@router.get("/logs", response_model=LogTailResponse)
async def get_logs(
    size: int = Query(default=200, ge=1, le=1000),
) -> LogTailResponse:
    entries = sample_data.log_tail(size)
    # The tail is a window, not an archive. Saying so lets the viewer show
    # "older entries exist" instead of implying this is the whole history.
    return LogTailResponse(entries=entries, truncated=True)
