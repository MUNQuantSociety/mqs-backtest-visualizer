"""Schema creation for the ``app`` namespace.

No Alembic yet, on purpose: the schema is days old and nobody outside this repo
depends on it, so ``CREATE SCHEMA IF NOT EXISTS`` plus ``create_all`` is honest
and cheap. Migrations arrive with the first change that has to preserve data
(recorded as deferred work in the plan).

Two entry points because two kinds of caller need it:

* :func:`init_database` — synchronous, for scripts, tests, and worker startup;
* :func:`ensure_schema` — async and run-once, so the API creates its tables
  before serving the first query that needs them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import Engine, text

from src.db.engine import create_sync_engine, get_async_engine
from src.models import APP_SCHEMA, Base

logger = logging.getLogger(__name__)

_CREATE_SCHEMA = text(f'CREATE SCHEMA IF NOT EXISTS "{APP_SCHEMA}"')

# Set once the tables have been confirmed to exist in this process. The lock
# keeps concurrent first requests from racing into create_all together.
_schema_ready = False
_schema_lock: asyncio.Lock | None = None


def init_database(engine: Engine | None = None) -> None:
    """Create the ``app`` schema and every table on it. Idempotent."""
    owned = engine is None
    engine = engine or create_sync_engine()
    try:
        with engine.begin() as connection:
            connection.execute(_CREATE_SCHEMA)
            Base.metadata.create_all(connection)
        logger.info("app schema ready (%d tables)", len(Base.metadata.tables))
    finally:
        if owned:
            engine.dispose()


async def ensure_schema() -> None:
    """Create the schema once per API process, on the async engine.

    ``server.py`` runs this from the lifespan hook, so in a normally-started
    app every call from the request path costs one boolean read. It stays on
    the request path anyway because the lifespan is allowed to fail (a database
    that is down at boot must not stop the app from serving ``/live/*``), and
    then the first request that needs a table is what creates it.
    """
    global _schema_ready, _schema_lock
    if _schema_ready:
        return

    if _schema_lock is None:
        _schema_lock = asyncio.Lock()

    async with _schema_lock:
        if _schema_ready:
            return
        async with get_async_engine().begin() as connection:
            await connection.execute(_CREATE_SCHEMA)
            await connection.run_sync(Base.metadata.create_all)
        _schema_ready = True
        logger.info("app schema ready (%d tables)", len(Base.metadata.tables))


def reset_schema_ready_flag() -> None:
    """Forget that the schema was checked. Only tests should need this."""
    global _schema_ready
    _schema_ready = False


@asynccontextmanager
async def database_lifespan(_app: object = None) -> AsyncIterator[None]:
    """FastAPI lifespan hook: schema up front, pool closed on shutdown.

    A failure to reach the database is logged, not raised. The alternative is
    an app that refuses to start when the university network is down — taking
    ``/live/*``, which needs no database at all, down with it. ``_schema_ready``
    stays false in that case, so :func:`ensure_schema` retries on the first
    request that actually needs a table.
    """
    from src.db.engine import dispose_async_engine

    try:
        await ensure_schema()
    except Exception as exc:  # pragma: no cover - needs an unreachable server
        logger.warning(
            "Could not create the app schema at startup (%s: %s); "
            "the first database-backed request will retry.",
            type(exc).__name__,
            exc,
        )
    try:
        yield
    finally:
        await dispose_async_engine()
