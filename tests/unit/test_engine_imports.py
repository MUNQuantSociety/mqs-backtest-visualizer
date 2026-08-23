"""Guardrails on the vendored engine's shape. No database, no market data.

These tests exist because the engine was *copied* out of another repository.
The two ways that copy silently rots are (a) an import path sneaking back to
MQSMaster's layout and (b) the engine picking up a dependency on the web
application it is supposed to be independent of. Both are cheap to check and
expensive to discover at run time in a worker process.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import engine
from engine.contracts import (
    METRIC_KEYS,
    EquityPoint,
    NoMarketData,
    RunCancelled,
    RunRequest,
    RunResult,
)
from engine.run_single import load_strategy_class
from engine.strategies.portfolio_BASE.strategy import BasePortfolio, _camel_to_snake

ENGINE_ROOT = Path(engine.__file__).resolve().parent
ENGINE_SOURCES = sorted(ENGINE_ROOT.rglob("*.py"))

# Anything the engine must not depend on: the web application it serves, the
# ORM that application uses, and the progress bar that a browser replaced.
FORBIDDEN_ROOTS = {"src", "fastapi", "starlette", "sqlalchemy", "tqdm", "portfolios"}


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by one module, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_engine_sources_exist() -> None:
    assert len(ENGINE_SOURCES) > 25, "the vendored copy looks incomplete"


@pytest.mark.parametrize("path", ENGINE_SOURCES, ids=lambda p: p.name)
def test_engine_module_imports_nothing_forbidden(path: Path) -> None:
    offenders = _imported_roots(path) & FORBIDDEN_ROOTS
    assert not offenders, (
        f"{path.relative_to(ENGINE_ROOT)} imports {sorted(offenders)}; "
        "engine/ must stay runnable without src/ and without the web stack"
    )


def test_no_relative_imports_survive() -> None:
    """MQSMaster's try-relative-then-absolute idiom must not have come along."""
    survivors = [
        f"{path.relative_to(ENGINE_ROOT)}:{lineno}"
        for path in ENGINE_SOURCES
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"\s*from\s+\.", line)
    ]
    assert not survivors, f"relative imports left in the engine: {survivors}"


def test_engine_version_matches_vendored_from() -> None:
    provenance = (ENGINE_ROOT / "VENDORED_FROM").read_text(encoding="utf-8")
    short_sha = re.search(r"^short_sha:\s*(\w+)$", provenance, re.MULTILINE)
    assert short_sha, "VENDORED_FROM must record the upstream short SHA"
    assert engine.ENGINE_VERSION == f"vendored-{short_sha.group(1)}"


def test_every_strategy_keeps_its_config_beside_it() -> None:
    """BasePortfolio finds config.json as a sibling of the class's own file."""
    strategy_dirs = sorted(
        path
        for path in (ENGINE_ROOT / "strategies").glob("portfolio_*")
        if path.is_dir()
    )
    assert len(strategy_dirs) == 5
    for directory in strategy_dirs:
        assert (directory / "strategy.py").is_file(), directory
        assert (directory / "config.json").is_file(), directory


def test_indicator_modules_follow_the_dynamic_loading_convention() -> None:
    """snake_case filename -> CamelCase class, which AddIndicator relies on."""
    modules = sorted(
        path
        for path in (ENGINE_ROOT / "indicators").glob("*.py")
        if path.stem not in {"__init__", "base"}
    )
    assert modules, "no indicators were vendored"
    for path in modules:
        source = path.read_text(encoding="utf-8")
        expected = "".join(part.title() for part in path.stem.split("_"))
        classes = {
            node.name
            for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef)
        }
        match = {name for name in classes if _camel_to_snake(name) == path.stem}
        assert match, (
            f"{path.name} defines {sorted(classes)}; AddIndicator would look for "
            f"a class whose snake_case name is {path.stem} (e.g. {expected})"
        )


def test_load_strategy_class_accepts_both_spellings() -> None:
    colon = load_strategy_class(
        "engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy"
    )
    dotted = load_strategy_class(
        "engine.strategies.portfolio_dummy.strategy.CrossoverRmiStrategy"
    )
    assert colon is dotted
    assert issubclass(colon, BasePortfolio)


def test_load_strategy_class_rejects_a_non_strategy() -> None:
    with pytest.raises(TypeError):
        load_strategy_class("engine.core.executor:BacktestExecutor")
    with pytest.raises(ValueError):
        load_strategy_class("engine.core.executor:NoSuchClass")
    with pytest.raises(ValueError):
        load_strategy_class("   ")


def test_run_result_defaults_are_terminal_and_empty() -> None:
    result = RunResult(status="failed", error="boom")
    assert result.metrics == {}
    assert result.equity_curve == []
    assert result.fills == []
    assert result.final_equity is None


def test_run_request_defaults_are_process_safe() -> None:
    """The callback defaults must be usable with no worker attached."""
    request = RunRequest(
        run_id="r1",
        strategy_key="portfolio_dummy",
        class_path="engine.strategies.portfolio_dummy.strategy:CrossoverRmiStrategy",
        start_date="2026-01-02",
        end_date="2026-01-31",
        initial_capital=100_000.0,
    )
    assert request.mode == "event"
    assert request.params == {}
    assert request.should_cancel() is False
    assert request.on_progress(50, "simulating") is None


def test_equity_point_unpacks_as_a_tuple() -> None:
    from datetime import date

    point = EquityPoint(date=date(2026, 1, 2), equity=101.5, benchmark=None)
    day, equity, benchmark = point
    assert (day, equity, benchmark) == (date(2026, 1, 2), 101.5, None)


def test_metric_keys_cover_the_run_metrics_columns() -> None:
    assert METRIC_KEYS == (
        "total_return",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "volatility",
        "win_rate",
        "profit_factor",
        "total_trades",
    )


def test_no_market_data_names_the_tickers_and_the_window() -> None:
    error = NoMarketData(["AAPL", "ZZZZ"], "2026-01-02", "2026-01-31")
    message = str(error)
    assert "AAPL" in message and "ZZZZ" in message
    assert "2026-01-02" in message and "2026-01-31" in message
    assert isinstance(error, Exception)
    assert issubclass(RunCancelled, Exception)
