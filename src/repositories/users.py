"""Idempotent first-sign-in mapping; never link accounts by email or legacy ID."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AppUser


async def get_or_create_user(session: AsyncSession, *, issuer: str, subject: str) -> AppUser:
    lookup = select(AppUser).where(AppUser.issuer == issuer, AppUser.subject == subject)
    existing = (await session.execute(lookup)).scalar_one_or_none()
    if existing is not None:
        return existing
    statement = (
        insert(AppUser).values(issuer=issuer, subject=subject)
        .on_conflict_do_nothing(constraint="uq_users_issuer_subject")
        .returning(AppUser)
    )
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is not None:
        return user
    # Under READ COMMITTED, the conflicting transaction is committed before
    # INSERT returns; this next statement observes the single winning identity.
    return (await session.execute(lookup)).scalar_one()


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> AppUser:
    """Serialize the one-time onboarding decision for one application user."""
    statement = (
        select(AppUser)
        .where(AppUser.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()
