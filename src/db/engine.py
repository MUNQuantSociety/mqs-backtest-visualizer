"""Engine and session plumbing — the only place a connection is created.

Two engines, because the process that serves HTTP and the process that runs a
backtest have opposite needs:

* the API is async (asyncpg) and holds a small pool for the lifetime of the app;
* workers, scripts, and schema creation are plain synchronous psycopg2, created
  and disposed per use — a pool inherited across a ``ProcessPoolExecutor`` fork
  or Windows spawn is a corrupted socket waiting to happen.

Both are lazy. Importing this module must never open a socket: pytest collects
it on machines with no database, and Windows spawn re-imports it in every
worker process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_async_engine() -> AsyncEngine:
    """The API's engine. Created on first use, reused thereafter."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            settings.database_url_async,
            # The database is remote and on a university network; connections
            # go stale between requests and pre-ping turns a mid-request
            # OperationalError into a transparent reconnect.
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            future=True,
        )
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(),
            expire_on_commit=False,  # responses are serialised after commit
            autoflush=False,
        )
    return _async_session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """An async session with commit-on-success, rollback-on-error semantics.

    Services open one of these per operation. Routes never see it — that is
    what keeps SQLAlchemy out of the HTTP layer.
    """
    factory = get_async_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_async_engine() -> None:
    """Close the API pool. Called from a shutdown hook and by tests."""
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
    _async_engine = None
    _async_session_factory = None


@contextmanager
def detached_async_engine() -> Iterator[None]:
    """Give the enclosing block a private API engine, then restore the caller's.

    Exists for synchronous tests that drive async code with ``asyncio.run``:
    each such call is a new event loop, and an asyncpg pool belongs to the loop
    that opened it. Without this, one test file disposing "the" engine reaches
    into whatever another file left in these globals — the suite then passes
    only in the order pytest happens to collect it, and any reordering
    (``-k``, ``xdist``, ``pytest-randomly``) closes a pool a live client is
    still using.

    The block is expected to dispose the engine it created before it returns;
    this only guarantees it cannot dispose one it did not.
    """
    global _async_engine, _async_session_factory
    saved_engine, saved_factory = _async_engine, _async_session_factory
    _async_engine, _async_session_factory = None, None
    try:
        yield
    finally:
        _async_engine, _async_session_factory = saved_engine, saved_factory


def create_sync_engine() -> Engine:
    """A fresh synchronous engine for a worker, script, or schema creation.

    Deliberately not cached: the caller owns it and disposes it. Sharing one
    across process boundaries is the classic way to get "SSL error: decryption
    failed" from a pool of file descriptors two processes both think they own.
    """
    return create_engine(
        settings.database_url_sync,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        future=True,
    )


@contextmanager
def sync_session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Synchronous counterpart of :func:`session_scope` for workers/scripts."""
    owned = engine is None
    engine = engine or create_sync_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        if owned:
            engine.dispose()
