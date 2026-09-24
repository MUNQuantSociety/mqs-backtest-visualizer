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
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from engine.analytics.reporting import (
    _generate_buy_and_hold_benchmark,
    benchmark_weights,
    compute_metrics_dict,
    market_timestamps,
)
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
from engine.core.sentiment_gate import SentimentGate
from engine.data.db_adapter import EngineDBAdapter
from engine.data.fmp import FMPDataAdapter, market_data_source
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


class _ReportBacktestEngine(BacktestEngine):
    """Capture report inputs through existing hooks without changing execution."""

    def _build_fast_portfolio_stub(self, portfolio_class, config_data):
        stub = super()._build_fast_portfolio_stub(portfolio_class, config_data)
        self.fast_benchmark_weights = benchmark_weights(
            config_data.get("WEIGHTS"), stub.tickers
        )
        return stub

    def _fetch_fast_daily_close_data(self, portfolio_instance, start_date, end_date, tickers=None):
        prices = super()._fetch_fast_daily_close_data(
            portfolio_instance, start_date, end_date, tickers
        )
        self.fast_benchmark_prices = prices
        return prices


def _ny_dates(timestamps: pd.Series) -> pd.Series:
    """Exchange dates; naive daily labels are New York dates, never UTC."""
    return market_timestamps(timestamps).dt.date


def _equity_curve(
    perf_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    initial_capital: float | None = None,
) -> list[EquityPoint]:
    """Align benchmark marks as of each sample, then label with New York dates.

    The optional first point is an explicit pre-trading capital baseline on
    the first observed date. It is not a market bar or an engine return sample;
    report_metadata identifies it so daily persistence can keep it separately.
    """
    if perf_df is None or perf_df.empty:
        return []
    frame = pd.DataFrame({
        "timestamp": market_timestamps(perf_df["timestamp"]),
        "equity": pd.to_numeric(perf_df["portfolio_value"], errors="coerce"),
    })
    if frame.isna().any().any() or not frame["equity"].map(math.isfinite).all():
        raise EngineError("The engine produced invalid performance timestamps or equity.")
    frame = frame.sort_values("timestamp", kind="stable")
    frame["benchmark"] = float("nan")
    if benchmark_df is not None and not benchmark_df.empty:
        marks = benchmark_df[["timestamp", "buy_and_hold_value"]].copy()
        marks["timestamp"] = market_timestamps(marks["timestamp"])
        marks = marks.sort_values("timestamp", kind="stable").drop_duplicates("timestamp", keep="last")
        frame = pd.merge_asof(
            frame.drop(columns="benchmark"), marks.rename(columns={"buy_and_hold_value": "benchmark"}),
            on="timestamp", direction="backward",
        )
    points = [
        EquityPoint(
            date=row.timestamp.date(), equity=float(row.equity),
            benchmark=float(row.benchmark) if pd.notna(row.benchmark) else None,
        )
        for row in frame.itertuples(index=False)
    ]
    if initial_capital is not None and points:
        points.insert(0, EquityPoint(
            date=points[0].date, equity=float(initial_capital),
            benchmark=float(initial_capital) if points[0].benchmark is not None else None,
        ))
    return points


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
    """Attach each daily fast result to the last observed quote on that NY date."""
    perf_df = engine.last_fast_perf_df
    if perf_df is None or perf_df.empty:
        return None
    prices = getattr(engine, "fast_benchmark_prices", None)
    if prices is None or prices.empty:
        raise EngineError("Fast mode did not expose observed prices for its report.")
    moments = market_timestamps(prices["timestamp"])
    last_quotes = moments.groupby(moments.dt.date).max()
    frame = perf_df[["timestamp", "portfolio_value"]].copy()
    frame["timestamp"] = _ny_dates(frame["timestamp"]).map(last_quotes)
    if frame["timestamp"].isna().any():
        raise EngineError("Fast mode produced a daily result without an observed market bar.")
    return frame


def _execution_summary(
    mode: str, fills: list, diagnostics: dict | None, gate: SentimentGate | None = None
) -> dict:
    """Explain recorded fills without treating vector positions as no trades."""
    if mode == "fast":
        message = (
            "Fast mode models positions without individual order fills; an empty "
            "trade table does not mean the strategy made no trades."
        )
    elif fills:
        message = None
    else:
        message = "No trades were filled during the selected period."
        if diagnostics:
            requests = diagnostics.get("buyRequestCount", 0) + diagnostics.get("sellRequestCount", 0)
            if not diagnostics.get("evaluationCount", 0) and diagnostics.get("warmupSkipCount", 0):
                message = (
                    "No trades were placed: the strategy never had enough ready "
                    "market and indicator history to evaluate a signal."
                )
            elif requests:
                message = "The strategy requested trades, but none produced a fill during the selected period."
            elif diagnostics.get("evaluationCount", 0):
                message = (
                    "No trades were placed: no buy or sell requests were generated "
                    "during the selected period."
                )
                if not diagnostics.get("bullishSignalCount", 0):
                    message += " No ticker exceeded the strategy's bullish entry threshold."
        if gate is not None and gate.blocked_entry_count:
            message += (
                f" The sentiment gate blocked {gate.blocked_entry_count} long "
                "entries while recent news was below its threshold."
            )
        message += " Trade metrics that require executed or closed trades are unavailable."
    return {"fillCount": len(fills), "message": message}


def run_single(request: RunRequest) -> RunResult:
    """Execute one backtest and return its results as a :class:`RunResult`.

    Never raises for an expected failure: a crash, a cancellation, or a window
    with no market data all come back as a terminal ``RunResult`` so the caller
    has exactly one code path for "the run is over".
    """
    artifact_dir = _resolve_artifact_dir(request)
    mode = (request.mode or "event").lower().strip()
    adapter = None

    try:
        if mode not in {"event", "fast"}:
            raise EngineError(f"Unsupported backtest mode {mode!r}; use event or fast.")
        slippage = float(request.slippage)
        commission_per_share = float(request.commission_per_share)
        if not math.isfinite(slippage) or not 0 <= slippage < 1:
            raise EngineError("slippage must be a finite fraction between 0 (inclusive) and 1 (exclusive).")
        if not math.isfinite(commission_per_share) or commission_per_share < 0:
            raise EngineError("commission_per_share must be finite and nonnegative.")
        if mode == "fast" and commission_per_share:
            raise EngineError(
                "Fast mode does not support per-share commission; use event mode "
                "or explicitly set commission_per_share to zero."
            )
        if mode == "fast" and request.sentiment_gate is not None:
            raise EngineError("Fast mode does not support the sentiment gate; use event mode.")
        strategy_class = load_strategy_class(request.class_path)
        if mode == "fast":
            # Checked here, before the engine loads a single bar: a student who
            # picked the wrong mode should be told in milliseconds, not after
            # the run has occupied a worker slot for the length of a data load.
            _reject_unsupported_fast_mode(strategy_class)

        adapter = FMPDataAdapter() if market_data_source() == "fmp" else EngineDBAdapter()
        engine = _ReportBacktestEngine(
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
            slippage=slippage,
            # RunRequest exposes explicit price slippage and cash commission.
            # A legacy CostModel would replace that slippage inside the executor.
            cost_model=None,
            commission_per_share=commission_per_share,
            sentiment_gate=request.sentiment_gate,
            backtest_mode=mode,
        )

        request.on_progress(0, "starting")
        engine.run()

        benchmark_df = None
        strategy_diagnostics = None
        final_prices: dict[str, float] = {}
        if mode == "fast":
            perf_df = _fast_mode_perf(engine)
            # Fast mode is vectorized: there is no order book, so there are no
            # fills to report — the trade table stays empty by construction,
            # and with nothing to mark there are no final prices either.
            fills: list[dict[str, Any]] = []
            if perf_df is None:
                prices = getattr(engine, "fast_benchmark_prices", None)
                if prices is None or prices.empty:
                    raise NoMarketData(
                        tickers=strategy_tickers(strategy_class, request.params),
                        start=request.start_date, end=request.end_date,
                        reason="fast mode returned no observed daily prices",
                    )
                raise EngineError(
                    f"Fast mode produced no performance records for {request.strategy_key}. "
                    "Check the requested date window and vectorized strategy output."
                )
            prices = engine.fast_benchmark_prices
            weights = engine.fast_benchmark_weights
            benchmark_start = request.start_date
        else:
            runner = engine.last_runner
            perf_df = runner.perf_df if runner is not None else None
            fills = list(runner.executor.trade_log) if runner and runner.executor else []
            if runner is not None:
                strategy_diagnostics = getattr(runner.portfolio, "strategy_diagnostics", None)
                final_prices = dict(runner.final_prices)
                benchmark_df = runner.benchmark_df
                prices = runner.main_data_df
                weights = benchmark_weights(
                    getattr(runner.portfolio, "portfolio_weights", None),
                    getattr(runner.portfolio, "tickers", None),
                )
                benchmark_start = runner.backtest_loop_start_date

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

        if benchmark_df is None or benchmark_df.empty:
            benchmark_df = _generate_buy_and_hold_benchmark(
                prices, float(request.initial_capital), weights,
                start=benchmark_start, end=market_timestamps(perf_df["timestamp"]).max(),
            )
        benchmark_metadata = dict(benchmark_df.attrs.get("report_metadata", {}))
        if not benchmark_metadata:
            benchmark_metadata = {
                "kind": "configured_universe_buy_and_hold",
                "weights": weights, "universe": list(weights),
                "coverage": "unavailable",
                "missing_tickers": [ticker for ticker, weight in weights.items() if weight],
            }
        benchmark_metadata["price_sampling"] = "daily_close" if mode == "fast" else "engine_observed_bars"
        if mode == "fast" and not benchmark_df.empty:
            # This canonical artifact replaces the draft's reliance on the
            # vector engine's legacy, rebalanced equal-weight comparison.
            benchmark_df.to_csv(Path(artifact_dir) / "benchmark_buy_and_hold.csv", index=False)
        equity_curve = _equity_curve(perf_df, benchmark_df, float(request.initial_capital))
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
            final_prices=final_prices,
            report_metadata={
                "marketData": {"source": market_data_source(), "resolution": "daily"},
                "execution": _execution_summary(
                    mode, fills, strategy_diagnostics, request.sentiment_gate
                ),
                "sentimentGate": (
                    request.sentiment_gate.report()
                    if request.sentiment_gate is not None
                    else {"enabled": False}
                ),
                **({"strategyDiagnostics": strategy_diagnostics} if strategy_diagnostics is not None else {}),
                "executionCosts": {
                    "slippageFraction": slippage,
                    "commissionPerShare": commission_per_share,
                    "commissionBasis": "cash_per_filled_share_each_side",
                    "slippageBasis": "fill_price" if mode == "event" else "daily_weight_turnover",
                    "legacyCostModel": False,
                },
                "benchmark": benchmark_metadata,
                "equity": {
                    "mode": mode,
                    "timezone": "America/New_York",
                    "baseline": {
                        "curve_index": 0,
                        "date": equity_curve[0].date.isoformat(),
                        "value": float(request.initial_capital),
                        "kind": "initial_capital_before_trading",
                    },
                    "first_observation": market_timestamps(perf_df["timestamp"]).min().isoformat(),
                    "last_observation": market_timestamps(perf_df["timestamp"]).max().isoformat(),
                    "fast_strategy_semantics": (
                        "vectorized_approximation_with_warmup_positions_and_first_day_return"
                        if mode == "fast" else None
                    ),
                },
            },
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
        if adapter is not None:
            adapter.close()
