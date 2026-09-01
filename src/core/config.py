"""Typed application settings, parsed once from the environment.

Nothing else in the codebase reads ``os.environ`` — importing ``settings`` is
the only supported way to get configuration, so a typo fails at startup rather
than three screens deep at runtime.

This module is deliberately exhaustive: it carries every knob the whole backend
plan needs, including ones no code reads yet. Four feature lanes are built in
parallel against this file, so a missing setting forces a lane to edit shared
state and collide with its siblings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL

# Repository root — the parent of ``src/``. Relative path settings resolve
# against this rather than the process working directory, because worker
# processes and one-off scripts are launched from wherever the operator
# happens to be standing.
REPO_ROOT = Path(__file__).resolve().parents[2]

# ``override=False`` so a real environment variable (CI, container, systemd)
# always beats the developer's local ``.env`` file.
load_dotenv(REPO_ROOT / ".env", override=False)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_first(*names: str, default: str = "") -> str:
    """First non-empty value among several variable names.

    The deploy stack (MQS_AWS_INFRA) injects the database credentials under the
    names the .env template used before it was rewritten — MARKET_DATA_HOST and
    friends — while this codebase reads POSTGRES_*. Until the two repositories
    agree, accepting both means a deploy wired against either naming reaches
    the database instead of booting with no credentials and failing on the
    first backtest. POSTGRES_* wins when both are set.
    """
    for name in names:
        raw = os.getenv(name, "").strip()
        if raw:
            return raw
    return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _env_path(name: str, default: str) -> Path:
    """Resolve a path setting against the repo root unless it is absolute."""
    raw = os.getenv(name, "").strip() or default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (REPO_ROOT / candidate)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration. See ``.env.example`` for the full template."""

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_name: str = os.getenv("APP_NAME", "mqs-backtest-visualizer")
    app_env: str = os.getenv("APP_ENV", "development")
    debug: bool = _env_bool("DEBUG", True)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # The frontend calls the API through Vite's dev proxy, which makes requests
    # same-origin and means CORS is not exercised in normal local use. It is
    # still configured for the case where the app is served from a different
    # origin than the API.
    cors_origins: list[str] = field(
        default_factory=lambda: _split_csv(
            os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
        )
    )

    # Public path prefix. The frontend's VITE_API_BASE_URL is "/api", so every
    # route it calls resolves under this. Changing it breaks the client.
    api_prefix: str = os.getenv("API_PREFIX", "/api")

    repo_root: Path = REPO_ROOT

    # ------------------------------------------------------------------
    # PostgreSQL — the production MQS instance
    # ------------------------------------------------------------------
    # Read-only on ``public.market_data``; owner of the ``app`` schema. The
    # credentials are admin-level, so the boundary is a rule, not a grant.
    postgres_host: str = _env_first("POSTGRES_HOST", "MARKET_DATA_HOST")
    postgres_port: int = int(_env_first("POSTGRES_PORT", "MARKET_DATA_PORT", default="25060"))
    postgres_db: str = _env_first("POSTGRES_DB", "MARKET_DATA_DB", default="mqsdb")
    postgres_user: str = _env_first("POSTGRES_USER", "MARKET_DATA_USER")
    postgres_password: str = _env_first("POSTGRES_PASSWORD", "MARKET_DATA_PASSWORD")

    # The live server has SSL switched off (``SHOW ssl`` → off, checked
    # 2026-09-01), so ``require`` fails outright and ``prefer`` connects in
    # PLAINTEXT — the password crosses the network unencrypted. That is
    # tolerable on the university network and not across the public internet;
    # the deploy stack rightly insists on ``require``, which cannot succeed
    # until SSL is enabled on the CAIR instance. Nothing here should paper
    # over that: a required-SSL deploy must fail loudly, not downgrade.
    postgres_sslmode: str = _env_first(
        "POSTGRES_SSLMODE", "MARKET_DATA_SSLMODE", default="prefer"
    )

    # The API holds a handful of connections; the heavy lifting happens in
    # worker processes with their own short-lived sync connections.
    db_pool_size: int = _env_int("DB_POOL_SIZE", 5)
    db_max_overflow: int = _env_int("DB_MAX_OVERFLOW", 2)

    # Short by design: the database is remote (university network), and a
    # health probe or a skipped test must not hang a run for a minute.
    db_connect_timeout_seconds: int = _env_int("DB_CONNECT_TIMEOUT_SECONDS", 3)

    # Every bar timestamp the engine sees is normalized to this zone. The
    # engine's NY-trading-hours filter assumes it end to end.
    market_timezone: str = os.getenv("MARKET_TIMEZONE", "America/New_York")

    # ------------------------------------------------------------------
    # Run pipeline
    # ------------------------------------------------------------------
    # An event-mode backtest is minutes of single-core, GIL-holding work, so
    # concurrency is bounded by cores, not by request volume.
    max_concurrent_runs: int = _env_int("MAX_CONCURRENT_RUNS", 2)
    max_backtest_window_days: int = _env_int("MAX_BACKTEST_WINDOW_DAYS", 1825)

    # Progress and cancellation are polled from the worker on every timestamp
    # group; without a floor between writes the run would spend its time
    # talking to Postgres instead of simulating.
    progress_write_interval_seconds: float = _env_float(
        "PROGRESS_WRITE_INTERVAL_SECONDS", 1.0
    )

    # Worker liveness, separate from progress. Progress callbacks stop for
    # minutes while the engine loads bars from the remote database, so they
    # cannot say whether the worker is alive; a dedicated thread beats on this
    # cadence instead. A ``running`` row whose last beat is older than the
    # stale threshold is judged dead by the reconciler at boot. Keep stale >>
    # interval, with room for a slow database round trip.
    run_heartbeat_interval_seconds: float = _env_float(
        "RUN_HEARTBEAT_INTERVAL_SECONDS", 5.0
    )
    run_heartbeat_stale_seconds: float = _env_float(
        "RUN_HEARTBEAT_STALE_SECONDS", 90.0
    )

    # ------------------------------------------------------------------
    # User-strategy validation
    # ------------------------------------------------------------------
    # Validation executes user-supplied Python. The timeout is a wall-clock
    # backstop enforced through the normal cancellation path.
    validation_timeout_seconds: int = _env_int("VALIDATION_TIMEOUT_SECONDS", 600)

    # Validation only has to prove the strategy runs, so the window and the
    # capital are both kept small.
    validation_window_days: int = _env_int("VALIDATION_WINDOW_DAYS", 30)
    validation_initial_capital: float = _env_float(
        "VALIDATION_INITIAL_CAPITAL", 100_000.0
    )

    # ------------------------------------------------------------------
    # Storage locations (all gitignored, all created on demand)
    # ------------------------------------------------------------------
    artifact_dir: Path = _env_path("ARTIFACT_DIR", ".artifacts")
    market_cache_dir: Path = _env_path("MARKET_CACHE_DIR", "data/backfill_cache")

    strategy_store_backend: str = os.getenv("STRATEGY_STORE_BACKEND", "local").lower()
    strategy_store_root: Path = _env_path("STRATEGY_STORE_ROOT", ".strategy_store")
    # Unused until the infrastructure repo provisions a bucket; the S3 backend
    # is a stub that raises without it.
    strategy_store_s3_bucket: str = os.getenv("STRATEGY_STORE_S3_BUCKET", "")

    # ------------------------------------------------------------------
    # Derived connection URLs
    # ------------------------------------------------------------------
    # Both properties return a SQLAlchemy ``URL`` object rather than a string.
    # ``URL.create`` escapes the password correctly (ours contains characters
    # that hand-rolled interpolation and quoting get wrong), and ``URL``'s
    # repr masks the password, so an accidental log line cannot leak it.

    @property
    def database_url_async(self) -> URL:
        """asyncpg URL for the API process."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            # asyncpg has no ``sslmode`` argument — SQLAlchemy forwards query
            # keys straight to ``asyncpg.connect``, which would raise on it.
            # Its ``ssl`` argument accepts the identical libpq vocabulary
            # ("disable"/"allow"/"prefer"/"require"/...), so translate here.
            query={"ssl": self.postgres_sslmode},
        )

    @property
    def database_url_sync(self) -> URL:
        """psycopg2 URL for worker processes, scripts, and schema creation."""
        return URL.create(
            "postgresql+psycopg2",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            query={"sslmode": self.postgres_sslmode},
        )

    @property
    def psycopg2_connect_kwargs(self) -> dict[str, object]:
        """Arguments for a raw ``psycopg2.connect`` — no SQLAlchemy involved.

        The engine's DB adapter and the test-time reachability probe both talk
        to psycopg2 directly; they share this so connection details are
        described exactly once.
        """
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password,
            "sslmode": self.postgres_sslmode,
            "connect_timeout": self.db_connect_timeout_seconds,
        }

    @property
    def database_configured(self) -> bool:
        """False when the ``.env`` block is missing, so callers can say so."""
        return bool(self.postgres_host and self.postgres_user and self.postgres_db)


settings = Settings()
