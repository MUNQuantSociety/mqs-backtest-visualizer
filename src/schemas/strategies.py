"""Strategy response models.

Mirrors ``src/features/strategies/types.ts``. A strategy is the thing you test;
a backtest is one test of it.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, field_validator

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

    # Additive, so the client's Zod schema (which knows only active/draft/
    # archived) keeps parsing. ``validation_state`` is the registry's real
    # lifecycle value — ``validating``, ``active``, ``failed_validation``,
    # ``archived`` — and ``validation_run_id`` is the backtest that proves or
    # disproves an upload. Together they let the editor watch a submission
    # instead of waiting for it to appear in the catalogue.
    validation_state: str | None = None
    validation_run_id: str | None = None


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

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        """Trim before the length rules run, so ``"   "`` is empty, not valid.

        The client's Zod schema does ``.trim().min(1)``; without the same here a
        whitespace name slips past ``min_length`` and becomes a real row.
        """
        return value.strip() if isinstance(value, str) else value


class StrategySubmissionResult(CamelModel):
    id: str
    name: str
    status: StrategyStatus
    message: str = ""
    # The validation backtest queued for this upload. The message mentions it
    # in prose for a human; this field is the one a client can actually poll
    # (``GET /backtests/{id}``). Null only when the run could not be started.
    validation_run_id: str | None = None


class StrategyTemplate(CamelModel):
    """Starter source for the editor.

    Served rather than hardcoded in the client so the contract it teaches
    cannot drift from the engine that has to run it.
    """

    filename: str
    source: str


class StrategyCheckRequest(CamelModel):
    """Source to read for compatibility, and nothing else.

    No name and no description: this asks "would this run here?", it does not
    create anything, so the fields that identify a strategy are not needed yet.
    """

    source: str = Field(min_length=1)
    filename: str | None = None


class CompatibilityIssue(CamelModel):
    """One reason a file would not run, tied to the line that causes it.

    ``line`` is 0 when the problem is the file as a whole rather than any
    particular line, which the client renders without a line number.
    """

    line: int
    message: str


class CompatibilityStatus(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class StrategyCheckResult(CamelModel):
    """The verdict, always delivered with 200.

    Incompatible source is a *successful* check, not a failed request. The
    endpoint did exactly what it was asked to do and the answer is "no". A 4xx
    here would also collapse a list of problems into one ``detail`` string,
    which is the thing the check exists to avoid.
    """

    status: CompatibilityStatus
    ok: bool
    class_name: str | None = None
    issues: list[CompatibilityIssue] = []
    # Reported, never disqualifying: `ok` can be True with warnings present.
    warnings: list[CompatibilityIssue] = []
    message: str = ""
