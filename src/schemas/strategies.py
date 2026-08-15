"""Strategy response models.

Mirrors ``src/features/strategies/types.ts``. A strategy is the thing you test;
a backtest is one test of it.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field

from src.schemas.common import CamelModel

# Largest source file accepted, in bytes. Matches MAX_SOURCE_BYTES on the client
# so an oversized upload is rejected with the same limit at both ends.
MAX_SOURCE_BYTES = 256 * 1024


class StrategyStatus(str, Enum):
    ACTIVE = "active"
    DRAFT = "draft"
    ARCHIVED = "archived"


class ParameterSpec(CamelModel):
    """One tunable input, described well enough to render a form control."""

    key: str
    label: str
    type: Literal["number", "integer", "percent", "boolean"]
    default: float | bool
    min: float | None = None
    max: float | None = None


class Strategy(CamelModel):
    id: str
    name: str
    class_name: str
    description: str
    status: StrategyStatus
    tags: list[str] = []
    parameters: list[ParameterSpec] = []
    universe: list[str] = []

    # Aggregates over this strategy's runs, denormalised onto the row: the
    # catalogue would otherwise need one request per strategy to render a card.
    run_count: int = 0
    best_sharpe: float | None = None
    best_return: float | None = None
    last_run_at: str | None = None


class StrategyListResponse(CamelModel):
    items: list[Strategy]
    total: int


class StrategySubmission(CamelModel):
    """New strategy source, however the author supplied it.

    ``source`` is untrusted user code. It is stored as a draft and never
    imported or executed here — running it requires the sandboxed worker pool
    described in the platform plan (no network egress, CPU and wall-clock caps).
    """

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    source: str = Field(min_length=1)
    filename: str | None = None


class StrategySubmissionResult(CamelModel):
    id: str
    name: str
    status: StrategyStatus
    message: str = ""
