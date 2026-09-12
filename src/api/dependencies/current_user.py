"""Resolve an explicit identity or the configured temporary account.

Both paths require an existing public.user_creds row. With TEMPORARY_USER_ID
unset, requests still require X-User-Id. This is a stand-in for sign-in.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, status

from src.core.config import settings
from src.db.engine import session_scope
from src.repositories import user_creds as user_creds_repo

USER_ID_HEADER = "X-User-Id"


async def require_current_user(
    x_user_id: Annotated[str | None, Header(alias=USER_ID_HEADER)] = None,
) -> uuid.UUID:
    """The explicit or temporary user's ID, validated against user_creds."""
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
    return owner_id
