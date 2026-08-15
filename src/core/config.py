"""Typed application settings, parsed once from the environment.

Nothing else in the codebase reads ``os.environ`` — importing ``settings`` is
the only supported way to get configuration, so a typo fails at startup rather
than three screens deep at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime configuration. See ``.env.example`` for the full template."""

    app_name: str = os.getenv("APP_NAME", "mqs-backtest-visualizer")
    app_env: str = os.getenv("APP_ENV", "development")
    debug: bool = os.getenv("DEBUG", "true").lower() == "true"

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


settings = Settings()
