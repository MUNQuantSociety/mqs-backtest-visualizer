"""Database plumbing: engines, sessions, and schema creation."""

from src.db.engine import (
    create_sync_engine,
    dispose_async_engine,
    get_async_engine,
    get_async_session_factory,
    session_scope,
    sync_session_scope,
)
from src.db.init import database_lifespan, ensure_schema, init_database

__all__ = [
    "create_sync_engine",
    "database_lifespan",
    "dispose_async_engine",
    "ensure_schema",
    "get_async_engine",
    "get_async_session_factory",
    "init_database",
    "session_scope",
    "sync_session_scope",
]
