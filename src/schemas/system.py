"""System health and log tail models.

Mirrors ``src/features/system/types.ts``. A single green dot cannot tell you the
NLP daemon died while the trading engine kept running, and that distinction is
the reason the page exists — hence per-service state rather than one flag.
"""

from __future__ import annotations

from enum import Enum

from src.schemas.common import CamelModel


class ServiceState(str, Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Service(CamelModel):
    name: str
    label: str
    state: ServiceState
    detail: str | None = None
    last_heartbeat_at: str | None = None


class SystemStatus(CamelModel):
    # Worst state across all services — the headline the page leads with.
    state: ServiceState
    # ``start.sh`` checks market hours before launching the stack, so
    # "everything down" outside market hours is expected, not an incident.
    market_open: bool
    services: list[Service]
    version: str
    uptime_seconds: float
    checked_at: str


class LogEntry(CamelModel):
    id: str
    timestamp: str
    level: LogLevel
    logger: str
    message: str
    portfolio_id: str | None = None


class LogTailResponse(CamelModel):
    entries: list[LogEntry]
    # True when older entries exist beyond the tail window.
    truncated: bool = False
