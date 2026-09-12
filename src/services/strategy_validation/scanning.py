"""Reading an uploaded strategy without running it.

Two jobs on the same AST pass. :func:`scan_source` refuses source that must not
be stored, raising on the first problem. :func:`check_compatibility` reads the
same rules plus the engine's contract and reports every problem at once, which
is what ``POST /strategies/check`` answers with.

SECURITY: none of this is a sandbox. The scan reads source the interpreter is
about to execute anyway, and any author who wants past it can get past it with
a string, a dunder or a decorator. It stops accidents, not intent. Real
isolation (a container per run, no network egress, a database role scoped to
``public.market_data`` instead of the admin credentials the worker holds) is
required before this is exposed beyond the club.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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

# The base class an uploaded strategy must extend, by name. An AST scan reads
# names, not objects.
BASE_CLASS_NAME = "BasePortfolio"


class StrategyValidationError(ValueError):
    """An upload the student can fix, carrying the sentence to show them.

    The route turns this into a 422 whose ``detail`` is ``str(exc)`` verbatim,
    so every message here names the offending line and says what would be
    accepted instead.
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
                "relative imports are not allowed. An uploaded strategy is a "
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
                f"{value.id}.{node.attr} is not allowed. An uploaded strategy "
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
        # ``portfolio_BASE.strategy.BasePortfolio``, the dotted spelling.
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
        issues.extend(_indicator_issues(strategy))
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


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
#
# A strategy naming an indicator the engine does not have passes every other
# check here and then dies at construction with a ModuleNotFoundError. That is
# the last common way to get "compatible" from this file and a failed run from
# the worker, so the names are checked.
#
# The available set is discovered, not listed. Dropping a new file into
# engine/indicators makes it valid here with no edit, which is the only way a
# hardcoded list stays correct.


def _camel_to_snake(name: str) -> str:
    """The engine's own transform, copied so the two cannot disagree.

    ``AddIndicator`` turns the class name into a module name this way and then
    imports ``engine.indicators.<module>``. See ``portfolio_BASE/strategy.py``.
    """
    first = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", first).lower()


@lru_cache(maxsize=1)
def _indicator_directory() -> Path | None:
    """Where the engine keeps its indicators, without importing any of them.

    ``find_spec`` resolves the package without executing its modules, so this
    stays as cheap as the rest of the scan.
    """
    try:
        spec = importlib.util.find_spec("engine.indicators")
    except (ImportError, ValueError):  # pragma: no cover - engine is vendored
        return None
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    return Path(locations[0]) if locations else None


@lru_cache(maxsize=1)
def known_indicators() -> frozenset[str]:
    """Every indicator class the engine can load, read from source.

    Parsed rather than imported: these files pull in pandas, and this runs on a
    request. The base class is excluded, since a strategy cannot use it.
    """
    directory = _indicator_directory()
    if directory is None:
        return frozenset()

    names: set[str] = set()
    for path in directory.glob("*.py"):
        if path.name in {"__init__.py", "base.py"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - vendored source
            continue
        names.update(
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name != "Indicator"
        )
    return frozenset(names)


def _indicator_issues(strategy: ast.ClassDef) -> list[CompatibilityIssue]:
    """Names passed to AddIndicator or RegisterIndicatorSet that do not exist.

    Only string literals are checked. A name built at run time cannot be read
    here, and guessing at one would refuse working code.
    """
    available = known_indicators()
    if not available:  # pragma: no cover - only if the engine is missing
        return []

    issues: list[CompatibilityIssue] = []
    for node in ast.walk(strategy):
        for name_node in _indicator_name_nodes(node):
            name = name_node.value
            if name in available:
                continue
            module = _camel_to_snake(name)
            issues.append(
                CompatibilityIssue(
                    name_node.lineno,
                    f"there is no indicator called {name!r}. The engine looks for "
                    f"engine/indicators/{module}.py and finds nothing. Available: "
                    f"{', '.join(sorted(available))}.",
                )
            )
    return issues


def _indicator_name_nodes(node: ast.AST) -> list[ast.Constant]:
    """The string literals naming an indicator in one call, if it is one.

    Two shapes, both used by the vendored strategies:
    ``AddIndicator("SimpleMovingAverage", ticker)`` and
    ``RegisterIndicatorSet({"fast": ("SimpleMovingAverage", {...})})``.
    """
    if not isinstance(node, ast.Call):
        return []

    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else None

    if name == "AddIndicator":
        first = node.args[0] if node.args else None
        return [first] if _is_str(first) else []

    if name == "RegisterIndicatorSet":
        found: list[ast.Constant] = []
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for value in arg.values:
                if isinstance(value, ast.Tuple) and value.elts:
                    head = value.elts[0]
                    if _is_str(head):
                        found.append(head)
        return found

    return []


def _is_str(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


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


