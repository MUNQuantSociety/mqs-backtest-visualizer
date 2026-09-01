"""Upload guardrails and validation-by-backtest for user strategies.

A student uploads a ``.py`` file. Nothing about that file is trusted, and the
only way to know whether it works is to run it — so this module does two jobs:
it scans the source before storing it, and it starts a *normal* backtest run
against it once stored.

The second job is the important design point. A validation run is not a
separate code path: it is a row in ``app.backtest_runs`` with
``purpose='validation'``, submitted to the same job manager, executed by the
same worker, reporting the same progress into the same columns. The student can
open it like any other run, and when it finishes the worker flips the strategy
to ``active``. Anything else would mean maintaining two run pipelines and
having the wrong one break silently.

SECURITY — READ THIS BEFORE CHANGING ANY OF IT
==============================================
Validating a strategy means **executing user-supplied Python** inside a worker
process that holds admin credentials to the production trading database. That
is a deliberate product decision (functional first, small trusted audience),
and it is the only reason the guardrails below are considered sufficient.

The guardrails are:

1. :func:`scan_source` — an AST scan that rejects imports outside an allowlist
   and the obvious escape hatches (``exec``, ``eval``, ``__import__``,
   ``open``, ``os``/``subprocess`` usage). :func:`check_compatibility` is the
   same reading plus the engine's own contract, reported as a list instead of
   raised on the first problem. It is what ``POST /strategies/check`` answers
   with, and it changes nothing about what is or is not accepted.
2. :func:`start_validation` — a wall-clock timeout that asks the run to cancel
   through the ordinary cancellation flag.
3. A short validation window, so a validation run is a minute of CPU rather
   than an hour of it.

**None of these is a security boundary.** The scan reads source that the
interpreter is about to execute anyway; any author who wants to get past it
can, with a string, a dunder, or a decorator — this is a speed bump against
accidents and casual mischief, nothing more. The timeout is cooperative: it
sets a flag the engine polls, and code that never returns to the engine loop
never sees it. Real isolation is deferred work and is required before this is
exposed beyond the club: a container per run, no network egress, and a database
role scoped to ``public.market_data`` instead of the admin credentials the
worker holds today.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from engine import ENGINE_VERSION
from src.core.config import settings
from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.integrations.strategy_store import get_strategy_store, strategy_key
from src.repositories import runs as runs_repo
from src.repositories import strategies as strategies_repo
from src.repositories.runs import TERMINAL_STATUSES
from src.schemas.backtests import BacktestStatus, BacktestSummary
from src.services.backtests import (
    MODE_KEY,
    _dispatch,
    _symbol_for,
    create_backtest_run,
)

logger = logging.getLogger(__name__)

# The two objects that make up a stored strategy. They match
# ``engine/strategies/user_loader.py`` — spelled again rather than imported
# because importing the loader would drag pandas into every process that only
# wants to write a file.
SOURCE_FILENAME = "strategy.py"
CONFIG_FILENAME = "config.json"

# ---------------------------------------------------------------------------
# The source scan — a speed bump, not a sandbox (see the module docstring)
# ---------------------------------------------------------------------------

# Top-level packages an uploaded strategy may import. Everything a strategy
# legitimately needs is here: the engine's own API, the two numeric libraries
# it is written against, and the handful of stdlib modules that compute rather
# than reach outside the process.
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "engine",
        "pandas",
        "numpy",
        "math",
        "datetime",
        "typing",
        "collections",
        "statistics",
        # Not in the plan's list, added deliberately: every vendored strategy
        # (including the template a student copies) logs, and rejecting the
        # import would reject the example we hand out.
        "logging",
    }
)

# Names that turn "source we scanned" into "source we did not". Rejected
# wherever they appear, not just when called, because ``run = exec`` defeats a
# call-site-only check with one line.
BANNED_NAMES = frozenset(
    {"exec", "eval", "compile", "__import__", "open", "input", "breakpoint"}
)

# Modules whose *use* is refused even though the import allowlist already
# refuses them: a strategy that reaches one of these through an attribute it
# was handed is doing something a backtest never needs to do.
BANNED_MODULE_ROOTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "requests",
        "urllib",
        "importlib",
        "builtins",
        "ctypes",
        "pickle",
        "pathlib",
    }
)

# The standard routes from any object back to the interpreter's innards. No
# strategy needs them; a student who wanted around the scan would start here.
BANNED_ATTRIBUTES = frozenset(
    {
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__code__",
        "__loader__",
        "__mro__",
        "__import__",
    }
)

# The base class an uploaded strategy must extend, by name — an AST scan reads
# names, not objects.
BASE_CLASS_NAME = "BasePortfolio"


class StrategyValidationError(ValueError):
    """An upload the student can fix, carrying the sentence to show them.

    The route turns this into a 422 whose ``detail`` is ``str(exc)`` verbatim,
    so every message here names the offending line and says what would be
    accepted instead.
    """


class ValidationStartError(RuntimeError):
    """Validation could not be *started* — a server-side problem, not a bad upload.

    Kept apart from :class:`StrategyValidationError` because the upload itself
    was fine: the source is stored and the registry row exists, and the student
    is told the strategy could not be validated yet rather than that their code
    is wrong.
    """


@dataclass(frozen=True)
class SourceScan:
    """What the scan learned about an accepted upload."""

    class_name: str


def scan_source(source: str) -> SourceScan:
    """Reject an upload that must not be executed, or report its class.

    Raises :class:`StrategyValidationError` naming the line at fault. This runs
    before anything is stored, so a rejected upload leaves no trace at all.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        line = exc.lineno or 0
        raise StrategyValidationError(
            f"Line {line}: the file is not valid Python ({exc.msg})."
        ) from None

    for node in ast.walk(tree):
        violation = _violation(node)
        if violation is not None:
            raise StrategyValidationError(f"Line {node.lineno}: {violation}")

    return SourceScan(class_name=_sole_strategy_class_name(tree))


def _violation(node: ast.AST) -> str | None:
    """The reason this node is refused, or None if it is allowed."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                return _import_refusal(alias.name)
        return None

    if isinstance(node, ast.ImportFrom):
        if node.level:
            # A relative import has nothing to be relative to: an upload is a
            # single file, materialized on its own into a temporary directory.
            return (
                "relative imports are not allowed — an uploaded strategy is a "
                "single file with no package around it."
            )
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_IMPORT_ROOTS:
            return _import_refusal(node.module or "")
        return None

    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return (
            f"{node.id!r} is not allowed in an uploaded strategy. A strategy "
            "computes from the data it is given; it does not load code or "
            "touch files."
        )

    if isinstance(node, ast.Attribute):
        if node.attr in BANNED_ATTRIBUTES:
            return f"the attribute {node.attr!r} is not allowed in an uploaded strategy."
        value = node.value
        if isinstance(value, ast.Name) and value.id in BANNED_MODULE_ROOTS:
            return (
                f"{value.id}.{node.attr} is not allowed — an uploaded strategy "
                "may not reach the operating system, the filesystem, or the "
                "network."
            )
    return None


def _import_refusal(module: str) -> str:
    allowed = ", ".join(sorted(ALLOWED_IMPORT_ROOTS))
    return (
        f"importing {module!r} is not allowed. An uploaded strategy may import "
        f"only: {allowed}."
    )


def _sole_strategy_class_name(tree: ast.AST) -> str:
    """The name of the one ``BasePortfolio`` subclass in the file.

    Exactly one, because the run pipeline has to know what to instantiate
    without asking: zero means the file is not a strategy, and two means the
    answer depends on which one the loader happens to find first.
    """
    names = [node.name for node in _strategy_class_nodes(tree)]

    if not names:
        raise StrategyValidationError(
            f"This file defines no {BASE_CLASS_NAME} subclass. A strategy is a "
            f"class that inherits from {BASE_CLASS_NAME} and implements "
            "OnData(self, context)."
        )
    if len(names) > 1:
        raise StrategyValidationError(
            f"This file defines {len(names)} strategies ({', '.join(sorted(names))}). "
            f"Upload one {BASE_CLASS_NAME} subclass per file so there is no "
            "question which one to run."
        )
    return names[0]


def _extends_base_portfolio(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == BASE_CLASS_NAME:
            return True
        # ``portfolio_BASE.strategy.BasePortfolio`` — the dotted spelling.
        if isinstance(base, ast.Attribute) and base.attr == BASE_CLASS_NAME:
            return True
    return False

# ---------------------------------------------------------------------------
# The compatibility check: the same reading, reported instead of raised
# ---------------------------------------------------------------------------
#
# ``POST /strategies/check`` answers one question before a student commits to
# an upload: would this file run here? It reuses the scan above, then asks the
# two things the scan does not, because the scan's job is "is this safe enough
# to store" and this one's job is "does this satisfy the engine's contract".
#
# It never raises for bad source. A file with three problems should list three
# problems. An editor that reports one mistake per round trip makes fixing a
# strategy a guessing game.

# The one method the engine calls on a strategy. ``BasePortfolio`` declares it
# abstract, so a class without it cannot even be instantiated.
ONDATA_METHOD = "OnData"

# Spellings that mean the author wrote the right method with the wrong name.
# Worth naming explicitly: the capital O is unusual for Python and it is the
# single most likely reason a correct-looking strategy does not run.
ONDATA_MISSPELLINGS = frozenset({"on_data", "ondata", "onData", "On_Data"})


@dataclass(frozen=True)
class CompatibilityIssue:
    """One reason a file would not run, tied to the line that causes it.

    ``line`` is 0 when the problem is the file as a whole (no strategy class in
    it, for instance) rather than any particular line.
    """

    line: int
    message: str


@dataclass(frozen=True)
class CompatibilityReport:
    """Everything the check learned, whether or not the file passed."""

    compatible: bool
    class_name: str | None
    issues: tuple[CompatibilityIssue, ...]
    # Things that will probably break at run time but might not. Reported so
    # the author sees them, never enough on their own to refuse the file.
    warnings: tuple[CompatibilityIssue, ...]


def check_compatibility(source: str) -> CompatibilityReport:
    """Read a strategy the way the platform will, and report what it found.

    Deliberately static: nothing here imports, compiles or executes the source,
    so the check is safe to run on every keystroke-sized request and costs a
    millisecond. That also bounds what it can prove: it establishes that the
    file *can* be loaded and has the right shape, not that the strategy makes
    money or even trades. Proving it runs is what the validation backtest that
    :func:`start_validation` queues is for.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return CompatibilityReport(
            compatible=False,
            class_name=None,
            issues=(
                CompatibilityIssue(
                    line=exc.lineno or 0,
                    message=f"the file is not valid Python ({exc.msg}).",
                ),
            ),
            warnings=(),
        )

    issues: list[CompatibilityIssue] = []
    warnings: list[CompatibilityIssue] = []

    # Same reading as the upload scan, so a file that passes the check cannot
    # then be refused by ``POST /strategies`` for a reason it was not told.
    for node in ast.walk(tree):
        violation = _violation(node)
        if violation is not None:
            issues.append(
                CompatibilityIssue(getattr(node, "lineno", 0), violation)
            )

    class_name = None
    classes = _strategy_class_nodes(tree)

    if not classes:
        issues.append(
            CompatibilityIssue(
                0,
                f"this file defines no {BASE_CLASS_NAME} subclass. A strategy is "
                f"a class that inherits from {BASE_CLASS_NAME} and implements "
                f"{ONDATA_METHOD}(self, context).",
            )
        )
    elif len(classes) > 1:
        names = ", ".join(sorted(node.name for node in classes))
        issues.append(
            CompatibilityIssue(
                classes[1].lineno,
                f"this file defines {len(classes)} strategies ({names}). Upload "
                f"one {BASE_CLASS_NAME} subclass per file so there is no "
                "question which one to run.",
            )
        )
    else:
        strategy = classes[0]
        class_name = strategy.name
        issues.extend(_contract_issues(strategy))
        warnings.extend(_contract_warnings(strategy))

    issues = _deduplicated(issues)
    warnings = _deduplicated(warnings)

    return CompatibilityReport(
        compatible=not issues,
        class_name=class_name,
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def _deduplicated(issues: list[CompatibilityIssue]) -> list[CompatibilityIssue]:
    """Line order, one entry per distinct complaint.

    ``os.path.join(os.path.dirname(__file__))`` is three refusals of the same
    thing on one line, and reading "3 problems" when there is one is worse than
    useless. Sorting is stable, so two genuinely different problems on the same
    line stay in the order the file makes them.
    """
    seen: set[tuple[int, str]] = set()
    unique: list[CompatibilityIssue] = []
    for issue in issues:
        marker = (issue.line, issue.message)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(issue)
    unique.sort(key=lambda issue: issue.line)
    return unique


def _strategy_class_nodes(tree: ast.AST) -> list[ast.ClassDef]:
    """Every ``BasePortfolio`` subclass in the file, in source order.

    Inheritance is followed through classes defined in the same file, not just
    direct ``class X(BasePortfolio)`` lines. An author who factors a shared base
    out of two strategies writes ``class MyBase(BasePortfolio)`` and then
    ``class Actual(MyBase)``, and the loader counts both because it asks
    ``issubclass``. Counting only the direct one made this file look like a
    single strategy that forgot ``OnData``, which named the wrong class and
    pointed at the wrong fix.
    """
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    # Fixed point rather than one pass: ``class C(B)`` may appear above
    # ``class B(BasePortfolio)``, and Python does not care about the order.
    subclass_names = {node.name for node in classes if _extends_base_portfolio(node)}
    while True:
        grown = {
            node.name
            for node in classes
            if node.name not in subclass_names
            and any(
                isinstance(base, ast.Name) and base.id in subclass_names
                for base in node.bases
            )
        }
        if not grown:
            break
        subclass_names |= grown

    return [node for node in classes if node.name in subclass_names]


def _contract_issues(strategy: ast.ClassDef) -> list[CompatibilityIssue]:
    """What stops the engine from driving this class, if anything.

    Only ``OnData`` is checked, because only ``OnData`` is required:
    ``BasePortfolio`` declares it abstract and the run loop calls it once per
    bar. Everything else a strategy might define is optional.
    """
    method = _method_named(strategy, ONDATA_METHOD)

    if method is None:
        # ``OnData = some_function`` binds a perfectly good method; the engine
        # runs it. Nothing about the target can be read from the assignment, so
        # the honest answer is to accept it and say nothing about its signature
        # rather than to reject working code.
        if _assigns_attribute(strategy, ONDATA_METHOD):
            return []

        misspelled = next(
            (
                candidate
                for name in ONDATA_MISSPELLINGS
                if (candidate := _method_named(strategy, name)) is not None
            ),
            None,
        )
        if misspelled is not None:
            return [
                CompatibilityIssue(
                    misspelled.lineno,
                    f"{strategy.name}.{misspelled.name} should be spelled "
                    f"{ONDATA_METHOD}. The engine calls that exact name, "
                    "capital O and capital D.",
                )
            ]
        return [
            CompatibilityIssue(
                strategy.lineno,
                f"{strategy.name} does not implement {ONDATA_METHOD}(self, "
                f"context). The engine calls it once per bar; a strategy "
                "without it cannot be instantiated.",
            )
        ]

    issues: list[CompatibilityIssue] = []

    if isinstance(method, ast.AsyncFunctionDef):
        issues.append(
            CompatibilityIssue(
                method.lineno,
                f"{ONDATA_METHOD} is declared 'async def'. The engine calls it "
                "synchronously inside the bar loop and would never await the "
                "coroutine it returns.",
            )
        )

    # The engine calls ``self.OnData(context)``, so what the definition needs
    # depends on what the descriptor does with the receiver: a plain method and
    # a classmethod are handed one implicitly, a staticmethod is not.
    # ``*args`` satisfies any of them: unusual in a strategy, but it does
    # receive the argument, and refusing working code for its signature would
    # be wrong.
    required = 1 if _has_decorator(method, "staticmethod") else 2
    positional = len(method.args.posonlyargs) + len(method.args.args)
    if positional < required and method.args.vararg is None:
        expected = (
            f"{ONDATA_METHOD}(context)"
            if required == 1
            else f"{ONDATA_METHOD}(self, context)"
        )
        issues.append(
            CompatibilityIssue(
                method.lineno,
                f"{ONDATA_METHOD} takes {positional} argument(s); the engine "
                f"calls {ONDATA_METHOD}(context), so it must be defined as "
                f"{expected}.",
            )
        )

    return issues


def _has_decorator(
    method: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> bool:
    """True if ``method`` carries the bare decorator ``name``."""
    return any(
        isinstance(decorator, ast.Name) and decorator.id == name
        for decorator in method.decorator_list
    )


def _assigns_attribute(strategy: ast.ClassDef, name: str) -> bool:
    """True if the class body binds ``name`` with an assignment."""
    for node in strategy.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return True
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                return True
    return False


def _contract_warnings(strategy: ast.ClassDef) -> list[CompatibilityIssue]:
    """Shapes that usually work but have one well-known way of not working."""
    initializer = _method_named(strategy, "__init__")
    if initializer is None or _calls_super_init(initializer):
        return []

    return [
        CompatibilityIssue(
            initializer.lineno,
            f"{strategy.name}.__init__ never calls super().__init__(...). "
            f"{BASE_CLASS_NAME} is what reads config.json and sets self.tickers, "
            "self.logger and the indicator machinery, so skipping it usually "
            "fails on the first bar.",
        )
    ]


def _method_named(
    strategy: ast.ClassDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """A method defined directly on this class, not on anything nested in it."""
    for node in strategy.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_super_init(initializer: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if this ``__init__`` calls ``super().__init__`` anywhere inside."""
    for node in ast.walk(initializer):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "__init__"
            and isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "super"
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# The generated config
# ---------------------------------------------------------------------------

# The frontend's upload form sends name, description, source and filename and
# nothing else, so the configuration a strategy needs is generated. Two liquid
# large caps keep a validation run short; a student who wants a different
# universe re-runs the strategy from the catalogue once it is active.
DEFAULT_TICKERS = ("AAPL", "MSFT")
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_LOOKBACK_DAYS = 30
# Uppercase because that is the engine's vocabulary: the runner keys its data
# dictionary with "MARKET_DATA", and a lower-case spelling reaches no feed.
DEFAULT_DATA_FEEDS = ("MARKET_DATA",)

# The one parameter an upload advertises to the run form. Deliberately not
# TICKERS: overriding the ticker list without also overriding WEIGHTS produces
# a run whose benchmark and risk outputs are misaligned with what it traded.
LOOKBACK_PARAM_SPEC: dict[str, Any] = {
    "key": "LOOKBACK_DAYS",
    "label": "Lookback (days)",
    "type": "integer",
    "default": DEFAULT_LOOKBACK_DAYS,
    "min": 5,
    "max": 365,
}


def build_config(strategy_key_value: str) -> dict[str, Any]:
    """The ``config.json`` stored beside an upload's source.

    Same shape as a built-in portfolio's config, because the engine reads it
    with the same code: ``BasePortfolio`` pulls PORTFOLIO_ID, TICKERS, WEIGHTS,
    INTERVAL, LOOKBACK_DAYS and DATA_FEEDS straight out of this dictionary.
    """
    weight = round(1.0 / len(DEFAULT_TICKERS), 6)
    return {
        "PORTFOLIO_ID": strategy_key_value,
        "TICKERS": list(DEFAULT_TICKERS),
        "WEIGHTS": {ticker: weight for ticker in DEFAULT_TICKERS},
        "INTERVAL": DEFAULT_INTERVAL_SECONDS,
        "LOOKBACK_DAYS": DEFAULT_LOOKBACK_DAYS,
        "DATA_FEEDS": list(DEFAULT_DATA_FEEDS),
    }


def parameter_specs() -> list[dict[str, Any]]:
    """The catalogue's parameter form for an upload."""
    return [dict(LOOKBACK_PARAM_SPEC)]


def store_strategy_source(key: str, source: str, config: dict[str, Any]) -> str:
    """Write an upload into the strategy store and return its storage key.

    Source and config go in together because the engine needs them together:
    ``BasePortfolio`` finds its config by looking beside the file its class was
    defined in, so a key holding only ``strategy.py`` materializes into a
    directory the engine cannot configure.
    """
    storage = strategy_key(key)
    store = get_strategy_store()
    store.put(storage, SOURCE_FILENAME, source)
    store.put(storage, CONFIG_FILENAME, json.dumps(config, indent=2) + "\n")
    return storage


def discard_stored_source(key: str) -> None:
    """Remove everything stored for a strategy. Missing is not an error."""
    try:
        get_strategy_store().delete(strategy_key(key))
    except Exception:
        # Deleting the registry row is what the caller asked for and it has
        # already happened; an object left in the store is unreachable, not
        # broken.
        logger.exception("Stored source for strategy %s could not be removed", key)


# ---------------------------------------------------------------------------
# The validation run
# ---------------------------------------------------------------------------


async def validation_window(tickers: list[str]) -> tuple[date, date]:
    """The short window a validation run executes over.

    Anchored on the last bar the universe actually has, never on today: market
    data ends weeks behind the calendar, so a window computed from ``now()``
    returns zero rows and would fail every upload for a reason that has nothing
    to do with the uploaded code.
    """
    async with session_scope() as session:
        latest = await strategies_repo.latest_market_data_date(session, tickers)

    if latest is None:
        raise ValidationStartError(
            "there is no market data for "
            f"{', '.join(tickers)}, so there is no window to validate over"
        )

    span = max(int(settings.validation_window_days), 1)
    return latest - timedelta(days=span), latest


async def start_validation(
    *, strategy_key_value: str, strategy_name: str, tickers: list[str]
) -> BacktestSummary:
    """Queue the backtest that proves an upload works.

    An ordinary run in every respect except ``purpose='validation'``, which is
    what tells the worker to write the outcome back onto the strategy row and
    keeps it out of the catalogue's run aggregates.

    ``submit_backtest_run`` is not reused because it hardcodes
    ``purpose='user'`` and validates a client-supplied request; there is no
    client request here. The dispatch half *is* reused, because a refused
    worker pool has to be handled identically either way.
    """
    start, end = await validation_window(tickers)

    summary = await create_backtest_run(
        name=f"Validation — {strategy_name}"[:120],
        strategy_key=strategy_key_value,
        start_date=start,
        end_date=end,
        initial_capital=float(settings.validation_initial_capital),
        symbol=_symbol_for(tickers),
        engine_version=ENGINE_VERSION,
        # Event mode only: it is the dependable path, and a vectorized
        # approximation would prove nothing about the code the student wrote.
        params={MODE_KEY: "event"},
        purpose="validation",
    )

    dispatched = await _dispatch(summary)
    if dispatched.status is BacktestStatus.FAILED:
        raise ValidationStartError(
            "the worker pool would not accept the validation run; "
            "see the run for the reason"
        )

    _schedule_timeout(dispatched.id)
    return dispatched


# Strong references to the watchdogs in flight. ``asyncio`` keeps only weak
# references to tasks, so a timer nobody holds can be garbage collected
# mid-sleep and silently never fire.
_watchdogs: set[asyncio.Task] = set()


def _schedule_timeout(run_id: str) -> None:
    """Arm the wall-clock backstop for one validation run.

    Honest about what this is: a timer in the API process that sets the same
    ``cancel_requested`` flag a student's Cancel button sets. If the API
    restarts, the timer is gone and the run keeps going until the worker
    finishes with it — the startup reconciler is what cleans up after that. It
    is not a resource limit, and it cannot stop code that never returns to the
    engine's loop; only process isolation can do either.
    """
    timeout = float(settings.validation_timeout_seconds)
    if timeout <= 0:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - only outside the API process
        logger.warning(
            "No event loop to arm the validation timeout for run %s on", run_id
        )
        return

    task = loop.create_task(_cancel_when_overdue(run_id, timeout))
    _watchdogs.add(task)
    task.add_done_callback(_watchdogs.discard)


async def _cancel_when_overdue(run_id: str, timeout: float) -> None:
    """Sleep out the timeout, then ask an unfinished validation run to stop."""
    try:
        await asyncio.sleep(timeout)
        parsed = runs_repo.parse_run_id(run_id)
        if parsed is None:  # pragma: no cover - the id came from a created row
            return

        async with session_scope() as session:
            row = await runs_repo.get_run(session, parsed)
            if row is None or row.run.status in TERMINAL_STATUSES:
                return
            await runs_repo.request_cancel(session, parsed)

        logger.warning(
            "Validation run %s passed its %.0fs limit; cancellation requested",
            run_id,
            timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # The run is still going and will still finish; losing the backstop is
        # not worth an unhandled exception in a background task.
        logger.exception("Validation timeout for run %s could not be applied", run_id)


async def mark_validation_unstarted(key: str, reason: str) -> None:
    """Park an upload whose validation never got off the ground.

    Without this the strategy sits in ``validating`` forever, which reads to a
    student as "still working" for a run that does not exist.
    """
    try:
        async with session_scope() as session:
            await strategies_repo.set_validation_state(
                session, key, status="failed_validation", enabled=False
            )
    except Exception:
        logger.exception("Strategy %s could not be marked unvalidated (%s)", key, reason)


# ---------------------------------------------------------------------------
# Migration off the staging column
# ---------------------------------------------------------------------------


async def migrate_staged_sources() -> int:
    """Move any pre-store upload into the store. Returns how many moved.

    Before this pipeline existed, ``POST /strategies`` parked source in
    ``app.strategies.source_staging`` and did nothing with it. Such a row can
    never run: the worker loads uploads from the store and from nowhere else.
    This sweep copies the staged source into the store, points the row at it,
    and empties the column — after which the column stays empty forever,
    because nothing writes it any more.

    A validation run is *not* started for the migrated rows. They were uploaded
    before validation existed and their authors are not waiting on a result;
    starting a run each would mean an unbounded burst of backtests on the first
    upload after a deploy.
    """
    await ensure_schema()
    async with session_scope() as session:
        staged = await strategies_repo.strategies_with_staged_source(session)
        pending = [(row.key, row.source_staging or "") for row in staged]

    migrated = 0
    for key, source in pending:
        try:
            storage = store_strategy_source(key, source, build_config(key))
            async with session_scope() as session:
                await strategies_repo.adopt_staged_source(
                    session, key, storage_key=storage
                )
        except Exception:
            logger.exception("Staged source for strategy %s could not be migrated", key)
            continue
        migrated += 1

    if migrated:
        logger.info("Migrated %d staged strategy source(s) into the store", migrated)
    return migrated
