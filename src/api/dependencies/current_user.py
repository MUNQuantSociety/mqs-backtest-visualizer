"""Stand-in for sign-in: the client sends who they are pretending to be.

Until real auth lands, ``X-User-Id`` must be a row in ``public.user_creds``.
Missing or unknown is 401. Nothing uses this yet — the next change will
pass the UUID into the list endpoint as ``owner_id``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, status

from src.db.engine import session_scope
from src.repositories import user_creds as user_creds_repo

USER_ID_HEADER = "X-User-Id"


async def require_current_user(
    x_user_id: Annotated[str | None, Header(alias=USER_ID_HEADER)] = None,
) -> uuid.UUID:
    """The signed-in user's ``user_creds.id``, or 401."""
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Send {USER_ID_HEADER} with a public.user_creds id.",
        )
    try:
        owner_id = uuid.UUID(x_user_id.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{USER_ID_HEADER} must be a UUID.",
        ) from exc

    async with session_scope() as session:
        user = await user_creds_repo.get_user(session, owner_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No user_creds row for that id.",
        )
    return owner_id
