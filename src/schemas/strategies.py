"""Strategy response models.

Mirrors ``src/features/strategies/types.ts``. A strategy is the thing you test;
a backtest is one test of it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator

from src.schemas.common import CamelModel
from src.services.strategy_validation.scanning import known_indicators

# Largest source file accepted, in bytes. Matches MAX_SOURCE_BYTES on the client
# so an oversized upload is rejected with the same limit at both ends.
MAX_SOURCE_BYTES = 256 * 1024

# The fragment limit, checked before assembly. Smaller than the file limit on
# purpose: an OnData body is a method, and 64 KB of it is already far past
# anything the editor is for. The assembled file still faces MAX_SOURCE_BYTES.
MAX_BODY_BYTES = 64 * 1024

# Indicator rows per draft. One instance per ticker is built for each, so this
# is generous for anything the editor is for and still bounds the file size.
MAX_INDICATORS = 32

# Both halves of an indicator spec are written into Python source.
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


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
    # Indicator classes this strategy's INDICATORS block registers. Empty for
    # one that registers by hand, or whose source cannot be read. The run form
    # marks these as active in its (read-only) signal list.
    indicators: list[str] = []
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
    # The same starter expressed for the fragment editor: the OnData body on
    # its own, and the indicators as a spec. Derived from ``source``, never
    # written twice.
    body: str = ""
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    # The starter body reads `self.last_price`, which only exists if STATE
    # declares it — so the fragment seed has to carry this or the first
    # submission of an unedited template dies on its first bar.
    state: dict[str, Any] = Field(default_factory=dict)


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


class IndicatorParameter(CamelModel):
    """One keyword an indicator class reads, with its default.

    ``kind`` is what the editor renders: ``number`` params are the tuning knobs
    (``period``, ``displacement``), while ``string`` ones are column plumbing
    (``price_col``) whose defaults are already right and which a member should
    not be asked about.

    ``default`` is null when the class requires the value — ``kwargs.get("period")``
    with no fallback — so the form asks rather than inventing a number.
    """

    key: str
    default: float | str | None = None
    kind: Literal["number", "string"]


class IndicatorDefinition(CamelModel):
    """An indicator class and the parameters it accepts."""

    name: str
    parameters: list[IndicatorParameter] = []


class IndicatorCatalogue(CamelModel):
    """Every indicator class the engine can load, by name.

    Served because two places were guessing at it: the draft editor's indicator
    rows, and the run form's signal list — which had six entries hardcoded, two
    of which (MACD, Bollinger) the engine does not ship at all. A name the
    engine cannot load is a ModuleNotFoundError inside a backtest, so the list
    belongs with the engine that owns it.
    """

    items: list[IndicatorDefinition]
    total: int


class StrategySource(CamelModel):
    """A saved strategy's stored Python.

    Deliberately not ``StrategyTemplate``: the two answer different questions,
    and the template has grown a fragment half that means nothing here. Sharing
    a model would put empty ``body``/``indicators`` on every source response.
    """

    filename: str
    source: str
    # The fragment this was authored from, when it was authored as one. Null
    # for an uploaded file — there is no body to reopen, and the source above
    # is exactly what its author wrote.
    body: str | None = None
    indicators: list["IndicatorSpec"] | None = None
    state: dict[str, Any] | None = None


class IndicatorSpec(CamelModel):
    """One indicator to register: the attribute, the class, its parameters.

    ``attribute`` is what the body reads (``self.fast_sma[ticker]``), so it has
    to be a plain identifier — the assembler renders it into a class attribute
    and a name that is not one would produce a file that cannot parse.
    """

    attribute: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER_PATTERN)
    # Same pattern, for the same reason: both halves are rendered into Python
    # source, and a value that is not an identifier can close the string it is
    # written into and append class-body code of its own.
    indicator: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER_PATTERN)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("indicator")
    @classmethod
    def _known_to_the_engine(cls, value: str) -> str:
        """Reject a name the engine cannot load, here rather than at run time.

        A typo otherwise survives the check, reaches the validation backtest and
        raises ModuleNotFoundError at construction — which reads as a broken
        platform. Reported as a field error on the spec so the editor can mark
        the row the member got wrong.
        """
        available = known_indicators()
        if available and value not in available:
            raise ValueError(
                f"there is no indicator called {value!r}. "
                f"Available: {', '.join(sorted(available))}."
            )
        return value


class StrategyDraftRequest(CamelModel):
    """A fragment to check: the OnData body, and what to register for it."""

    # No `max_length` here on purpose: pydantic would reject an oversized body
    # before the handler runs, so the route's friendly 413 — whose `detail` the
    # editor renders verbatim — would be unreachable, and this endpoint would
    # answer differently from `/check` for the same failure.
    body: str
    # Bounded because every row adds to the assembled file, and the body limit
    # alone does not cap that.
    indicators: list[IndicatorSpec] = Field(default_factory=list, max_length=MAX_INDICATORS)
    # Attributes carried between bars, rendered as the class's `STATE`. The
    # starter fragment uses one, so a draft without this cannot run it.
    state: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None

    @field_validator("indicators")
    @classmethod
    def _attributes_are_unique(cls, value: list[IndicatorSpec]) -> list[IndicatorSpec]:
        """Two rows with one attribute would silently become one indicator."""
        seen = [spec.attribute for spec in value]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate indicator attribute(s): {', '.join(duplicates)}.")
        return value


class StrategyDraftSubmission(StrategyDraftRequest):
    """The same fragment, with the identity a registry row needs."""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


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
    # Set only for a draft check. The assembled file comes back so the editor
    # can show exactly what will run without owning a second assembler — this
    # repo already carries the scar of a duplicated template that drifted.
    assembled_source: str | None = None
    # 1-based line in ``assembledSource`` holding the fragment's line 1.
    body_offset: int | None = None
