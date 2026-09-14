"""Fragment authoring: an ``OnData`` body and an indicator spec become a file.

The editor asks a member for two things — the statements inside ``OnData``, and
the indicators to register — and this module turns them into the complete
strategy the rest of the platform already knows how to handle. Everything
around the body (imports, the class, the docstring, ``INDICATORS``, the
``def OnData`` line) is generated here.

Two things follow from that, and they are the reason this is a module rather
than a few lines inside the scanner:

**Line numbers must be translated.** :func:`~.scanning.check_compatibility`
reports absolute lines in the assembled file, and the member never saw that
file. An issue at assembled line 31 is *their* line 7, and reporting 31 is
worse than reporting nothing — it points into code they did not write.

**The scanner is not modified.** It keeps one job, judging a complete file, and
the fragment↔file translation lives here. Remapping is subtraction; it does not
belong inside a parser.

SECURITY: assembling is string manipulation, not a sandbox. Nothing here
executes the fragment, and a draft that survives assembly still faces the same
scan, the same store and the same validation backtest as an uploaded file. The
post-assembly tree assertions below exist to stop a fragment *escaping the
method it is supposed to be*, not to make untrusted code safe.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

from src.services.strategy_validation.scanning import (
    BASE_CLASS_NAME,
    CompatibilityIssue,
    CompatibilityReport,
    check_compatibility,
)

logger = logging.getLogger(__name__)

#: What the generated class is called. Fixed rather than derived from the
#: strategy's name: the name is free text a member types, and a class name has
#: to be an identifier. The registry key is what identifies a strategy anyway.
GENERATED_CLASS_NAME = "MyStrategy"

#: The body is written at column 0 and indented into a method, so every line
#: moves right by two levels: class body, then method body.
_BODY_INDENT = " " * 8

_PRELUDE = """from engine.strategies.order_interface import StrategyContext
from engine.strategies.portfolio_BASE.strategy import BasePortfolio


class {class_name}(BasePortfolio):
    \"\"\"{docstring}\"\"\"

    INDICATORS = {{{indicators}}}

    STATE = {state}

    def OnData(self, context: StrategyContext):
"""

#: A body of nothing at all still has to be a syntactically valid method.
_EMPTY_BODY = "pass"

#: Both halves of an indicator spec are rendered *into Python source*, so both
#: have to be identifiers. Without this an ``indicator`` of
#: ``'X", {}),\n    }\n    STATE = {"pwned": {}}\n    OTHER = {\n        "z": ("X'``
#: closes the dict and writes new class attributes — a file that parses, passes
#: every tree assertion, and is not the file the member described. The schema
#: validates these too; the assembler refuses to trust its caller.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class IndicatorSpec:
    """One registered indicator: the attribute, the class, its parameters.

    ``params`` is a mapping the engine passes to the indicator's constructor.
    It is rendered with :func:`repr`, so only JSON-shaped values survive — which
    is all the editor can produce.
    """

    attribute: str
    indicator: str
    params: dict[str, object]


@dataclass(frozen=True)
class StrategyDraft:
    """What a member actually wrote."""

    body: str
    indicators: tuple[IndicatorSpec, ...] = ()
    #: Attributes to carry between bars, rendered as the class's ``STATE``.
    #:
    #: ``BasePortfolio.__init__`` assigns one instance attribute per key, so a
    #: body reading ``self.last_price`` needs the name declared here. The
    #: starter fragment does exactly that: without this field the seeded body
    #: passes the check and then dies on the first bar of the validation run
    #: with an ``AttributeError`` no static check could have predicted.
    state: dict[str, object] | None = None
    docstring: str = "One sentence on what edge this is trying to capture."


@dataclass(frozen=True)
class Assembled:
    """A complete file, and where the member's fragment sits inside it."""

    source: str
    #: 1-based line in ``source`` holding the fragment's line 1.
    body_offset: int
    body_lines: int


class ScaffoldEscape(Exception):
    """The fragment broke out of the method it was supposed to fill.

    Raised rather than reported: an issue means "your code has a problem you
    can fix", and this means "the file this produced is not the shape this
    module guarantees". The draft is refused outright.
    """


def _render_indicators(indicators: tuple[IndicatorSpec, ...]) -> str:
    if not indicators:
        return ""

    seen: set[str] = set()
    for spec in indicators:
        for value, field in ((spec.attribute, "attribute"), (spec.indicator, "indicator")):
            if not _IDENTIFIER.match(value):
                raise ScaffoldEscape(
                    f"indicator {field} {value!r} is not a Python identifier."
                )
        if spec.attribute in seen:
            # Two specs with one attribute render two identical dict keys and
            # Python keeps the last, so one of the member's indicators would
            # vanish with nothing said about it.
            raise ScaffoldEscape(f"two indicators share the attribute {spec.attribute!r}.")
        seen.add(spec.attribute)
    # Sorted by attribute, which is what makes assembly byte-deterministic:
    # check assembles once and submit assembles again, and the scanner carries
    # an explicit invariant that a file passing the first cannot be refused by
    # the second. Iteration order varying between those two calls would break
    # that invariant in a way no bug report could explain.
    rendered = "".join(
        f'\n        "{spec.attribute}": ("{spec.indicator}", {spec.params!r}),'
        for spec in sorted(indicators, key=lambda spec: spec.attribute)
    )
    return f"{rendered}\n    "


def _normalise(body: str) -> list[str]:
    """The fragment's lines, with the incidental whitespace taken out.

    CRLF and lone CR become newlines and tabs become four spaces, so mixed
    indentation cannot desync the generated file.

    **Leading blank lines are kept.** They are in the member's textarea, so
    dropping them would shift every reported line number up by as many as they
    typed — the same misattribution this module exists to prevent, just
    smaller. Only trailing blanks go, and those cannot move anything above
    them.
    """
    text = body.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    lines = text.split("\n")

    while lines and not lines[-1].strip():
        lines.pop()

    return lines or [_EMPTY_BODY]


def assemble(draft: StrategyDraft) -> Assembled:
    """Build the complete strategy file this draft describes.

    Byte-deterministic for a given draft: same indicators, same order, same
    text. :func:`check_draft` and the submit path both call this, and they have
    to agree.
    """
    prelude = _PRELUDE.format(
        class_name=GENERATED_CLASS_NAME,
        docstring=draft.docstring.replace('"""', "'''"),
        indicators=_render_indicators(draft.indicators),
        # `repr` of a plain dict: the editor can only produce JSON-shaped
        # values, and unlike the indicator names this is never spliced into a
        # string literal, so it cannot close one.
        state=repr(dict(draft.state or {})),
    )

    lines = _normalise(draft.body)
    # Blank lines stay blank rather than becoming eight spaces of nothing.
    indented = [f"{_BODY_INDENT}{line}" if line.strip() else "" for line in lines]

    source = prelude + "\n".join(indented) + "\n"
    return Assembled(
        source=source,
        # The prelude ends with the ``def OnData`` line plus its newline, so
        # the fragment starts on the line after the prelude's last.
        body_offset=prelude.count("\n") + 1,
        body_lines=len(lines),
    )


def _assert_shape(assembled: Assembled) -> None:
    """Check the fragment stayed inside the method it was given.

    A body line at column 0 could otherwise close ``OnData`` and define
    module-level code — a second class, an import, anything. Uniform
    indentation makes that hard; these assertions are what make it *checked*.
    Every one of them is about the assembled tree, not about the text.
    """
    try:
        tree = ast.parse(assembled.source)
    except SyntaxError:
        # Not an escape: a syntax error is the member's to fix, and
        # check_compatibility reports it with a line this module then remaps.
        return

    body = [node for node in tree.body if not isinstance(node, ast.Import | ast.ImportFrom)]
    if len(body) != 1 or not isinstance(body[0], ast.ClassDef):
        raise ScaffoldEscape(
            "the body defines something outside the strategy class."
        )

    generated = body[0]
    if generated.name != GENERATED_CLASS_NAME or not any(
        isinstance(base, ast.Name) and base.id == BASE_CLASS_NAME
        for base in generated.bases
    ):
        raise ScaffoldEscape("the body replaced the generated strategy class.")

    methods = [
        node
        for node in generated.body
        # AsyncFunctionDef included: an injected ``async def`` is still a method
        # on the generated class, and filtering on FunctionDef alone made it
        # invisible here.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if [method.name for method in methods] != ["OnData"]:
        raise ScaffoldEscape("the body added or removed a method on the class.")

    # The statements *inside* the method, not the method node: its own
    # ``lineno`` is the generated ``def`` line, which is deliberately outside
    # the fragment span.
    span = range(assembled.body_offset, assembled.body_offset + assembled.body_lines)
    for statement in methods[0].body:
        for node in ast.walk(statement):
            line = getattr(node, "lineno", None)
            if line is not None and line not in span:
                raise ScaffoldEscape(
                    "the body extends past the method it was written in."
                )


def _remap(issue: CompatibilityIssue, assembled: Assembled) -> CompatibilityIssue:
    """Translate one issue from assembled-file lines to fragment lines."""
    line = issue.line

    # 0 already means "the file as a whole", which survives translation.
    if line == 0:
        return issue

    if assembled.body_offset <= line < assembled.body_offset + assembled.body_lines:
        return CompatibilityIssue(line=line - assembled.body_offset + 1, message=issue.message)

    # Outside the fragment: the generated scaffold is at fault, not the member.
    # Clamping this to line 1 would blame code they never wrote, which is
    # exactly the failure this module exists to prevent.
    logger.error(
        "AUTHORING | Issue at generated line %d, outside the fragment span "
        "(%d..%d): %s",
        line,
        assembled.body_offset,
        assembled.body_offset + assembled.body_lines - 1,
        issue.message,
    )
    return CompatibilityIssue(
        line=0,
        message=(
            f"the generated file is at fault, not your code: {issue.message} "
            "Please report this."
        ),
    )


def assemble_checked(draft: StrategyDraft) -> Assembled:
    """:func:`assemble`, with the structural assertions applied.

    This is what a caller that intends to *store and run* the result must use.
    :func:`assemble` alone is the raw text transform, and a submit path calling
    it directly would accept exactly the drafts the check endpoint refuses —
    the two must not be able to disagree about what is well formed.
    """
    assembled = assemble(draft)
    _assert_shape(assembled)
    return assembled


def check_draft(draft: StrategyDraft) -> CompatibilityReport:
    """Assemble, check, and report every line number as the member's own.

    The same verdict semantics as :func:`~.scanning.check_compatibility`:
    incompatible is a successful check, and the caller renders the issues.
    """
    assembled = assemble_checked(draft)
    report = check_compatibility(assembled.source)
    return CompatibilityReport(
        compatible=report.compatible,
        class_name=report.class_name,
        issues=tuple(_remap(issue, assembled) for issue in report.issues),
        warnings=tuple(_remap(warning, assembled) for warning in report.warnings),
    )
