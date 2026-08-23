"""Run exactly one portfolio and return its results as data.

This is the only entrypoint the rest of the application uses. Upstream's
workflow was "edit constants in main_backtest.py, run it, read the CSVs it
dropped on disk"; a web service needs the opposite — parameters in, structured
results out, progress and cancellation while it works, and a failure that is
unmistakably a failure.

Process safety matters here: this function runs inside a
``ProcessPoolExecutor`` worker, and on Windows those workers are *spawned*,
so this module is imported fresh in a process that shares nothing. It
therefore holds no module-level mutable state — every run builds its own
database adapter, its own engine instance, and its own portfolio object, and
drops them all before returning.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from engine.analytics.reporting import compute_metrics_dict
from engine.analytics.vector_strategy_adapters import ADAPTERS_BY_CLASSNAME
from engine.contracts import (
    EngineError,
    EquityPoint,
    NoMarketData,
    RunCancelled,
    RunRequest,
    RunResult,
)
from engine.core.backtest_engine import BacktestEngine
from engine.data.db_adapter import EngineDBAdapter
from engine.strategies.portfolio_BASE.strategy import BasePortfolio

logger = logging.getLogger(__name__)


def load_strategy_class(class_path: str) -> type[BasePortfolio]:
    """Import and return the strategy class named by ``class_path``.

    Accepts both ``"pkg.module:ClassName"`` and ``"pkg.module.ClassName"``.
    The two spellings exist because the strategy registry is seeded by another
    lane and either is a reasonable thing to store; refusing one of them would
    be a runtime failure discovered by a student, not by a test.
    """
    if not class_path or not class_path.strip():
        raise ValueError("class_path is empty; nothing to run.")

    path = class_path.strip()
    if ":" in path:
        module_name, _, class_name = path.partition(":")
    else:
        module_name, _, class_name = path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            f"class_path {class_path!r} is not a module path plus a class name."
        )

    module = importlib.import_module(module_name)
    try:
        strategy_class = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(
            f"{module_name} has no class named {class_name!r}."
        ) from exc

    if not (
        isinstance(strategy_class, type) and issubclass(strategy_class, BasePortfolio)
    ):
        raise TypeError(
            f"{class_path} is not a BasePortfolio subclass "
            f"(got {type(strategy_class).__name__})."
        )
    return strategy_class


def _resolve_artifact_dir(request: RunRequest) -> str:
    """Per-run output directory for the engine's CSVs; created if absent."""
    root = request.artifact_dir or os.path.join(".artifacts", str(request.run_id))
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _equity_curve(perf_df: pd.DataFrame) -> list[EquityPoint]:
    """Turn the runner's performance frame into ``(date, equity, benchmark)``.

    Timestamps are converted to New York calendar dates because that is the
    exchange day every bar in this engine belongs to, and the frontend charts
    trading days. Multiple samples can share a date — event mode records once
    per poll interval — and downsampling to one row per day is the caller's
    decision, not the engine's.

    ``benchmark`` is ``None``: the buy-and-hold comparison is written to
    ``benchmark_buy_and_hold.csv`` in the artifact directory but is computed on
    a minute grid that does not line up with these samples, so pretending
    otherwise here would produce a chart that lies.
    """
    if perf_df is None or perf_df.empty:
        return []
    if "timestamp" not in perf_df or "portfolio_value" not in perf_df:
        return []

    frame = perf_df[["timestamp", "portfolio_value"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["portfolio_value"] = pd.to_numeric(
        frame["portfolio_value"], errors="coerce"
    )
    frame = frame.dropna().sort_values("timestamp")

    local_dates = frame["timestamp"].dt.tz_convert("America/New_York").dt.date
    return [
        EquityPoint(date=day, equity=float(value), benchmark=None)
        for day, value in zip(local_dates, frame["portfolio_value"])
    ]


def strategy_tickers(strategy_class: type[BasePortfolio], params: dict) -> list[str]:
    """Tickers this strategy will trade, without instantiating it.

    Reads the same sibling ``config.json`` the engine loads, then applies the
    request's parameter overlay, so an error message can name the universe even
    when construction never got far enough to build a portfolio object.
    """
    try:
        config_path = Path(inspect.getfile(strategy_class)).parent / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    config.update(params or {})
    return [str(t) for t in config.get("TICKERS", [])]


def fast_mode_supported(strategy_class: type[BasePortfolio]) -> bool:
    """True when a vectorized adapter exists for this strategy class.

    Fast mode is not "the same backtest, quicker": it replays a hand-written
    vectorized approximation of the strategy over a price matrix, and that
    approximation has to have been written. Only four classes have one, and
    ``engine/analytics/vector_strategy_adapters.py`` still calls itself
    incomplete — so this is a real capability check, not a formality. The run
    submission endpoint should ask before accepting ``mode="fast"``.
    """
    return strategy_class.__name__ in ADAPTERS_BY_CLASSNAME


def _reject_unsupported_fast_mode(strategy_class: type[BasePortfolio]) -> None:
    """Fail a fast-mode run that cannot work, before it costs anything."""
    if fast_mode_supported(strategy_class):
        return
    supported = ", ".join(sorted(ADAPTERS_BY_CLASSNAME)) or "<none>"
    raise EngineError(
        f"Fast mode is not available for {strategy_class.__name__}: it needs a "
        "vectorized adapter registered in "
        "engine/analytics/vector_strategy_adapters.py "
        f"(only {supported} have one). Run this strategy in event mode."
    )


def _fast_mode_perf(engine: BacktestEngine) -> pd.DataFrame | None:
    """Normalize the fast path's frame to the event path's column names."""
    perf_df = engine.last_fast_perf_df
    if perf_df is None or perf_df.empty:
        return None
    return perf_df[["timestamp", "portfolio_value"]].copy()


def run_single(request: RunRequest) -> RunResult:
    """Execute one backtest and return its results as a :class:`RunResult`.

    Never raises for an expected failure: a crash, a cancellation, or a window
    with no market data all come back as a terminal ``RunResult`` so the caller
    has exactly one code path for "the run is over".
    """
    artifact_dir = _resolve_artifact_dir(request)
    mode = (request.mode or "event").lower().strip()
    adapter = EngineDBAdapter()

    try:
        strategy_class = load_strategy_class(request.class_path)
        if mode == "fast":
            # Checked here, before the engine loads a single bar: a student who
            # picked the wrong mode should be told in milliseconds, not after
            # the run has occupied a worker slot for the length of a data load.
            _reject_unsupported_fast_mode(strategy_class)

        engine = BacktestEngine(
            db_connector=adapter,
            backtest_output_root=artifact_dir,
            strict=True,
        )
        engine.config_overrides = dict(request.params or {})
        engine.on_progress = request.on_progress
        engine.should_cancel = request.should_cancel
        engine.setup(
            portfolio_classes=[strategy_class],
            start_date=str(request.start_date),
            end_date=str(request.end_date),
            initial_capital=float(request.initial_capital),
            slippage=float(request.slippage),
            backtest_mode=mode,
        )

        request.on_progress(0, "starting")
        engine.run()

        if mode == "fast":
            perf_df = _fast_mode_perf(engine)
            # Fast mode is vectorized: there is no order book, so there are no
            # fills to report — the trade table stays empty by construction.
            fills: list[dict[str, Any]] = []
            if perf_df is None:
                # The commonest cause by far, and the engine only logs it.
                raise EngineError(
                    f"Fast mode produced no results for {request.strategy_key}. "
                    "It needs a vectorized adapter registered for "
                    f"{strategy_class.__name__} in "
                    "engine/analytics/vector_strategy_adapters.py; run this "
                    "strategy in event mode instead."
                )
        else:
            runner = engine.last_runner
            perf_df = runner.perf_df if runner is not None else None
            fills = list(runner.executor.trade_log) if runner and runner.executor else []

        if perf_df is None or perf_df.empty:
            # The engine got past its own data guard but produced no samples,
            # which means the window held no bars on or after the start date.
            # Reporting that as a successful run with an empty chart is how a
            # student concludes the strategy "did nothing".
            raise NoMarketData(
                tickers=strategy_tickers(strategy_class, request.params),
                start=request.start_date,
                end=request.end_date,
                reason="the backtest produced no performance records",
            )

        equity_curve = _equity_curve(perf_df)
        metrics = compute_metrics_dict(perf_df, float(request.initial_capital))
        final_equity = float(equity_curve[-1].equity) if equity_curve else None

        request.on_progress(100, "completed")
        return RunResult(
            status="completed",
            error=None,
            metrics=metrics,
            equity_curve=equity_curve,
            fills=fills,
            final_equity=final_equity,
            artifact_dir=artifact_dir,
        )

    except RunCancelled as exc:
        logger.info("Run %s cancelled: %s", request.run_id, exc)
        return RunResult(
            status="cancelled",
            error=str(exc),
            artifact_dir=artifact_dir,
        )
    except Exception as exc:
        # The class name is part of the message on purpose: "NoMarketData: ..."
        # tells a student their window is empty, while a bare message would
        # look like an internal bug.
        logger.exception("Run %s failed: %s", request.run_id, exc)
        return RunResult(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            artifact_dir=artifact_dir,
        )
    finally:
        adapter.close()
