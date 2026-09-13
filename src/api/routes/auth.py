"""Authenticated identity used to isolate frontend sessions and cached reports."""

from fastapi import APIRouter, Depends, Response

from src.api.dependencies.current_user import require_authenticated_user
from src.schemas.auth import AuthUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=AuthUser)
async def me(response: Response, user: AuthUser = Depends(require_authenticated_user)) -> AuthUser:
    response.headers["Cache-Control"] = "private, no-store"
    return user
