"""The engine's single seam onto PostgreSQL.

MQSMaster reaches the database through ``MQSDBConnector``, a pooled connector
wired into the trading system's settings. Only one method of it matters to the
backtest engine — ``execute_query(sql, params, fetch)`` returning
``{"status": ..., "data": [dict-rows]}`` — so that contract is reproduced here
instead of vendoring the connector, its pool, and its coupling to another
repo's configuration.

Reading ``os.environ`` directly is the one sanctioned exception to the
"only src/core/config.py reads the environment" rule: ``engine/`` must stay
importable and runnable without ``src/`` (a standalone script, a worker
process, a future extraction of the engine into its own package), and it may
not import the settings object that would otherwise supply these values. The
variable names and defaults deliberately match ``src/core/config.py`` so both
halves of the application read one ``.env``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_env_file() -> None:
    """Fold ``.env`` into ``os.environ`` if python-dotenv is installed.

    Loading it is what makes ``python scripts/smoke_engine.py`` work with no
    other setup. ``override=False`` keeps a real environment variable ahead of
    the file, same as config.py.

    It happens here rather than at import time because it *mutates the
    process environment*, and this module is imported by every spawned worker
    on Windows — an import that reconfigures the process is exactly the kind of
    side effect a process pool turns into a hard-to-see bug. The adapter's
    constructor is the last moment before the values are needed, and it runs in
    the process that needs them.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is in requirements.txt
        logger.debug("python-dotenv not installed; relying on the ambient environment.")
        return
    load_dotenv(_ENV_FILE, override=False)


class EngineDBAdapter:
    """Minimal psycopg2 adapter with ``MQSDBConnector``'s return contract.

    One instance owns one connection and is **not** thread-safe or
    fork-safe: build it inside the process that runs the backtest.
    """

    def __init__(self, **overrides: Any) -> None:
        _load_env_file()
        self._conn_kwargs: dict[str, Any] = {
            "host": os.environ.get("POSTGRES_HOST", ""),
            "port": int(os.environ.get("POSTGRES_PORT", "25060")),
            "dbname": os.environ.get("POSTGRES_DB", "mqsdb"),
            "user": os.environ.get("POSTGRES_USER", ""),
            "password": os.environ.get("POSTGRES_PASSWORD", ""),
            # This server rejects sslmode=require; prefer negotiates TLS and
            # connects. Verified against the live instance — see BACKEND_PLAN
            # section 2 before "hardening" it.
            "sslmode": os.environ.get("POSTGRES_SSLMODE", "prefer"),
            "connect_timeout": int(
                os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")
            ),
        }
        self._conn_kwargs.update(overrides)
        self._connection: Any = None

    def _connect(self) -> Any:
        """Return a live connection, reconnecting if the last one died."""
        if self._connection is None or self._connection.closed:
            self._connection = psycopg2.connect(**self._conn_kwargs)
            # Backfill queries are read-only and long; autocommit keeps them
            # from holding a transaction open across a multi-minute run.
            self._connection.autocommit = True
        return self._connection

    def execute_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        fetch: bool | str = False,
    ) -> dict[str, Any]:
        """Run ``sql`` and return ``MQSDBConnector``'s result envelope.

        ``fetch`` is truthy (upstream callers pass ``True``, ``"all"`` or
        ``"one"``) when rows are wanted. Failures are reported in the envelope
        rather than raised, because every caller in the engine branches on
        ``status`` and treats an error as "no data" — raising here would turn a
        recoverable slab fetch into a dead run.
        """
        try:
            connection = self._connect()
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, tuple(params) if params is not None else None)
                rows: list[dict[str, Any]] = []
                if fetch:
                    if fetch == "one":
                        row = cursor.fetchone()
                        rows = [dict(row)] if row is not None else []
                    else:
                        rows = [dict(row) for row in cursor.fetchall()]
            return {"status": "success", "data": rows}
        except Exception as exc:
            # Drop the connection so the next call reconnects instead of
            # reusing a socket that may be in an unknown state.
            self.close()
            logger.error("Engine query failed: %s", exc)
            return {"status": "error", "message": str(exc), "data": []}

    def close(self) -> None:
        """Close the connection if one is open. Safe to call repeatedly."""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # pragma: no cover - closing a dead socket
                pass
            self._connection = None

    def __enter__(self) -> "EngineDBAdapter":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()
