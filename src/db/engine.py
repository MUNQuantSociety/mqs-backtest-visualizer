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


class NewsDatabaseNotConfigured(RuntimeError):
    """NEWS_POSTGRES_* is blank, so there is no live news database to read."""


# Postgres itself refuses writes on these connections: every transaction starts
# read-only, so an INSERT or UPDATE fails at the server rather than relying on
# the code never issuing one. The statement timeout bounds a slow query against
# the live host so it cannot hold a dashboard request open.
_NEWS_SERVER_SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "15000",
}
_NEWS_POOL_SIZE = 2
_NEWS_MAX_OVERFLOW = 1

_news_engine: AsyncEngine | None = None
_news_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_news_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions on the live news database. Created on first use.

    Raises:
        NewsDatabaseNotConfigured: NEWS_POSTGRES_* is blank.
    """
    global _news_engine, _news_session_factory
    if _news_session_factory is None:
        if not settings.news_database_configured:
            raise NewsDatabaseNotConfigured(
                "The live news database is not configured: set NEWS_POSTGRES_HOST, "
                "NEWS_POSTGRES_USER and NEWS_POSTGRES_PASSWORD."
            )
        _news_engine = create_async_engine(
            settings.news_database_url_async,
            pool_pre_ping=True,
            pool_size=_NEWS_POOL_SIZE,
            max_overflow=_NEWS_MAX_OVERFLOW,
            connect_args={
                "server_settings": _NEWS_SERVER_SETTINGS,
                "timeout": settings.db_connect_timeout_seconds,
            },
            future=True,
        )
        _news_session_factory = async_sessionmaker(
            bind=_news_engine, expire_on_commit=False, autoflush=False
        )
    return _news_session_factory


@asynccontextmanager
async def news_session_scope() -> AsyncIterator[AsyncSession]:
    """A read-only session on the live news database, always rolled back.

    Never commits: there is nothing to commit, and ending every transaction
    with a rollback means a write that somehow got past the server's read-only
    mode could still never persist.

    Raises:
        NewsDatabaseNotConfigured: NEWS_POSTGRES_* is blank.
    """
    session = get_news_session_factory()()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


async def dispose_news_engine() -> None:
    """Close the news pool."""
    global _news_engine, _news_session_factory
    if _news_engine is not None:
        await _news_engine.dispose()
    _news_engine = None
    _news_session_factory = None


async def dispose_async_engine() -> None:
    """Close the API pools. Called from a shutdown hook and by tests."""
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
    _async_engine = None
    _async_session_factory = None
    await dispose_news_engine()


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
    global _async_engine, _async_session_factory, _news_engine, _news_session_factory
    saved = (_async_engine, _async_session_factory, _news_engine, _news_session_factory)
    _async_engine, _async_session_factory = None, None
    _news_engine, _news_session_factory = None, None
    try:
        yield
    finally:
        _async_engine, _async_session_factory, _news_engine, _news_session_factory = saved


# A gated backtest reads years of articles per ticker in one go; that is a
# bigger read than a dashboard poll, so it gets a longer (still bounded) limit.
_NEWS_WORKER_STATEMENT_TIMEOUT_MS = 60_000


def create_news_sync_engine() -> Engine:
    """A fresh read-only synchronous engine on the live news database.

    For backtest workers, which load a run's article scores before it starts.
    Same read-only guarantee as the API's news sessions: Postgres refuses any
    write. Not cached, for the reason given on :func:`create_sync_engine`; the
    caller disposes it.

    Raises:
        NewsDatabaseNotConfigured: NEWS_POSTGRES_* is blank.
    """
    if not settings.news_database_configured:
        raise NewsDatabaseNotConfigured(
            "The live news database is not configured: set NEWS_POSTGRES_HOST, "
            "NEWS_POSTGRES_USER and NEWS_POSTGRES_PASSWORD."
        )
    return create_engine(
        settings.news_database_url_sync,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={_NEWS_WORKER_STATEMENT_TIMEOUT_MS}"
            ),
        },
        future=True,
    )


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
