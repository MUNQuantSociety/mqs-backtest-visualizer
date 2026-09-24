"""Bearer authentication, with an explicit local-only legacy testing opt-in."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from src.core.config import settings
from src.db.engine import session_scope
from src.repositories import user_creds as user_creds_repo
from src.schemas.auth import AuthUser
from src.services import auth as auth_service

USER_ID_HEADER = "X-User-Id"


async def require_authenticated_user(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias=USER_ID_HEADER)] = None,
) -> AuthUser:
    # A present but invalid bearer credential never falls back to a caller ID.
    if authorization is not None:
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise _unauthorized()
        try:
            return await auth_service.authenticate(
                parts[1], issuer=settings.auth_cognito_issuer,
                client_id=settings.auth_cognito_client_id,
            )
        except auth_service.InvalidAccessToken as exc:
            raise _unauthorized() from exc
        except auth_service.AuthenticationUnavailable as exc:
            raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.") from exc
    if not (
        settings.auth_allow_dev_identity
        and settings.app_env.lower() in {"development", "test"}
        and not settings.auth_cognito_issuer
        and not settings.auth_cognito_client_id
    ):
        raise _unauthorized()
    return await _development_user(x_user_id)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="A valid access token is required.",
                         headers={"WWW-Authenticate": "Bearer"})


async def _development_user(x_user_id: str | None) -> AuthUser:
    supplied = (x_user_id or "").strip()
    identity = supplied or settings.temporary_user_id
    if not identity:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Send {USER_ID_HEADER} with a public.user_creds id.",
        )
    try:
        owner_id = uuid.UUID(identity)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED if supplied else status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{USER_ID_HEADER} must be a UUID." if supplied else "TEMPORARY_USER_ID must be a valid UUID.",
        ) from exc

    async with session_scope() as session:
        user = await user_creds_repo.get_user(session, owner_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED if supplied else status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No user_creds row for that id." if supplied else "The configured temporary user does not exist.",
        )
    return AuthUser(id=owner_id)


async def require_current_user(user: AuthUser = Depends(require_authenticated_user)) -> uuid.UUID:
    """All report and upload authorization uses this application UUID."""
    return user.id


async def optional_current_user(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias=USER_ID_HEADER)] = None,
) -> uuid.UUID | None:
    """The caller's id when it can be established, otherwise None; never raises an auth error.

    For open routes that only *label* data per caller, such as the strategy
    catalogue's ``origin``. The catalogue must keep answering on an expired
    token or an identity-provider outage; the worst case is an own strategy
    labelled ``community``. Never use this to authorize anything.
    """
    try:
        user = await require_authenticated_user(authorization, x_user_id)
    except HTTPException:
        return None
    return user.id
