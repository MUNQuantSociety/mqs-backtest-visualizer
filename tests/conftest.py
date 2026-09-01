"""Shared pytest scaffolding.

Its only job is the ``db`` marker's escape hatch: tests that need the live MQS
PostgreSQL skip cleanly when it is unreachable (offline laptop, university
network down, CI without credentials) and run normally when it is not. Without
this, a full ``pytest -q`` on a plane is a wall of connection errors instead of
a green suite with skips.
"""

from __future__ import annotations

import pytest

from src.core.config import settings


def _redact(message: str) -> str:
    """Strip the password out of a driver error before it reaches a report."""
    password = settings.postgres_password
    return message.replace(password, "***") if password else message


def _probe_database() -> tuple[bool, str]:
    """Try one short-timeout connection. Returns (reachable, reason-if-not)."""
    if not settings.database_configured:
        return False, (
            "database not configured: POSTGRES_HOST / POSTGRES_USER / POSTGRES_DB "
            "are missing from the environment (copy .env.example to .env)"
        )

    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        return False, f"psycopg2 is not installed: {exc}"

    try:
        # connect_timeout comes from settings (3s) so an unreachable host fails
        # in seconds rather than blocking the whole session on a TCP timeout.
        connection = psycopg2.connect(**settings.psycopg2_connect_kwargs)
    except Exception as exc:
        detail = _redact(str(exc).strip().splitlines()[0] if str(exc).strip() else "")
        return False, (
            f"MQS PostgreSQL at {settings.postgres_host}:{settings.postgres_port} "
            f"is unreachable ({type(exc).__name__}: {detail})"
        )

    connection.close()
    return True, ""


@pytest.fixture(scope="session")
def database_available() -> tuple[bool, str]:
    """Session-wide reachability verdict — the connection is attempted once."""
    return _probe_database()


@pytest.fixture(scope="session")
def require_database(database_available: tuple[bool, str]) -> None:
    """Skip from inside a module- or session-scoped fixture.

    The autouse skip below is function-scoped, and pytest sets broader-scoped
    fixtures up first. A module-scoped fixture that opens a connection would
    therefore run before that skip ever fires: an offline machine gets a
    connection error instead of a skip, and an online one can burn a full
    backtest before printing "skipped".

    Depend on this from any fixture wider than function scope. That is the
    whole fix; deferred item B9 in the plan.
    """
    reachable, reason = database_available
    if not reachable:
        pytest.skip(reason)


@pytest.fixture(autouse=True)
def _skip_when_database_unreachable(request: pytest.FixtureRequest) -> None:
    """Skip ``db``-marked tests when the database cannot be reached.

    The marker check happens before the fixture is resolved, so a suite with no
    ``db`` tests selected never opens a socket at all.
    """
    if request.node.get_closest_marker("db") is None:
        return

    reachable, reason = request.getfixturevalue("database_available")
    if not reachable:
        pytest.skip(reason)
