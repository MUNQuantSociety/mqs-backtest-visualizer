"""Integration-test scaffolding: a signed-in user for the real app.

Every report and upload route requires an owner. Outside production the app
accepts ``X-User-Id`` naming a ``public.user_creds`` row, so integration tests
create a disposable row, send it on every request, and remove it afterwards.
``TEMPORARY_USER_ID`` is deliberately not used: ``test_public_run_list``
asserts that a request without the header is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from src.api.dependencies.current_user import USER_ID_HEADER
from src.db.engine import create_sync_engine

_INSERT_USER = text(
    "INSERT INTO public.user_creds (id, email, display_name) "
    "VALUES (:id, :email, 'Integration tests')"
)
_DELETE_USER = text("DELETE FROM public.user_creds WHERE id = :id")


@pytest.fixture(scope="session")
def integration_user_id(require_database: None) -> Iterator[uuid.UUID]:
    """A ``user_creds`` row that exists for this test session only."""
    user_id = uuid.uuid4()
    engine = create_sync_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                _INSERT_USER, {"id": user_id, "email": f"{user_id}@example.invalid"}
            )
        yield user_id
        with engine.begin() as connection:
            connection.execute(_DELETE_USER, {"id": user_id})
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def integration_user_headers(integration_user_id: uuid.UUID) -> dict[str, str]:
    """Headers that sign a ``TestClient`` in as :func:`integration_user_id`."""
    return {USER_ID_HEADER: str(integration_user_id)}
