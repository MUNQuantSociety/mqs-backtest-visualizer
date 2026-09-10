"""Lookups against ``public.user_creds``.

The table is owned outside this app (dummy users for the dashboard filter).
These queries never create or alter it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_GET_USER = text(
    """
    SELECT id, email, display_name
    FROM public.user_creds
    WHERE id = :user_id
    """
)


@dataclass(frozen=True)
class UserCred:
    id: uuid.UUID
    email: str
    display_name: str | None


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserCred | None:
    row = (await session.execute(_GET_USER, {"user_id": user_id})).mappings().first()
    if row is None:
        return None
    return UserCred(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
    )
