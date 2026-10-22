"""Raw PostgreSQL connections, scoped to one auth operation."""

import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from src.auth import config  # noqa: F401 -- load the repository .env


@contextmanager
def transaction():
    connection = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.getenv("POSTGRES_PORT", "25060")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.getenv("POSTGRES_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "3")),
        cursor_factory=RealDictCursor,
    )
    try:
        with connection:
            yield connection
    finally:
        connection.close()
