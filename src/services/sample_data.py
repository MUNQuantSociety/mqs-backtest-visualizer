"""In-memory stand-in for the database.

Every endpoint currently reads from here so the frontend can run against real
HTTP — real status codes, real serialisation, real error paths — before any
table exists. This module is the seam that gets deleted: routes call these
functions, so swapping in ``src/repositories/`` later touches nothing else.

Data is generated deterministically from a fixed seed. A chart that changes on
every refresh makes it impossible to tell a backend bug from noise.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from src.schemas.backtests import (
    BacktestDetail,
    BacktestStatus,
    BacktestSummary,
    EquityPoint,
    PerformanceMetrics,
    Trade,
)
from src.schemas.portfolios import (
    CompositionSeries,
    CorrelationMatrix,
    EngineState,
    EquitySamplePoint,
    EquitySeries,
    Execution,
    OmsConfig,
    PortfolioConfig,
    PortfolioDetail,
    PortfolioSummary,
    Position,
)
from src.schemas.strategies import ParameterSpec, Strategy, StrategyStatus
from src.schemas.system import LogEntry, LogLevel, Service, ServiceState, SystemStatus

SEED = 20260815
_EPOCH = datetime(2025, 1, 2, tzinfo=timezone.utc)

# Process start, used for the uptime figure on the status endpoint.
_STARTED_AT = datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _rng(salt: str) -> random.Random:
    """A generator seeded per subject, so one series never shifts another."""
    return random.Random(f"{SEED}:{salt}")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_STRATEGY_BLUEPRINTS: list[dict] = [
    {
        "id": "portfolio_1",
        "name": "Volume Momentum",
        "class_name": "VolumeMomentumStrategy",
        "description": "Ranks the universe by volume-weighted momentum and holds the leaders.",
        "tags": ["momentum", "volume"],
        "universe": ["AAPL", "MSFT", "NVDA", "AMZN"],
        "parameters": [
            {"key": "lookback_days", "label": "Lookback (days)", "type": "integer",
             "default": 90, "min": 20, "max": 365},
            {"key": "top_n", "label": "Positions held", "type": "integer",
             "default": 4, "min": 1, "max": 20},
        ],
    },
    {
        "id": "portfolio_2",
        "name": "Cross-Sectional Momentum",
        "class_name": "MomentumStrategy",
        "description": "Classic time-series momentum with a volatility-scaled position size.",
        "tags": ["momentum"],
        "universe": ["SPY", "TLT", "GLD", "XOM"],
        "parameters": [
            {"key": "lookback_days", "label": "Lookback (days)", "type": "integer",
             "default": 120, "min": 20, "max": 365},
            {"key": "vol_target", "label": "Volatility target", "type": "percent",
             "default": 0.15, "min": 0.05, "max": 0.4},
        ],
    },
    {
        "id": "portfolio_3",
        "name": "Regime Adaptive",
        "class_name": "RegimeAdaptiveStrategy",
        "description": "Switches allocation on a VIX-derived regime signal.",
        "tags": ["regime", "vix", "adaptive"],
        "universe": ["SPY", "TLT", "GLD", "_VIX"],
        "parameters": [
            {"key": "vix_threshold", "label": "VIX threshold", "type": "number",
             "default": 22.0, "min": 10.0, "max": 50.0},
            {"key": "risk_off_weight", "label": "Risk-off weight", "type": "percent",
             "default": 0.6, "min": 0.0, "max": 1.0},
        ],
    },
]


def list_strategies() -> list[Strategy]:
    """The catalogue, with per-strategy run aggregates folded in.

    Aggregates are derived from the backtest list rather than hardcoded, so a
    strategy card can never claim a Sharpe its own runs disagree with.
    """
    runs = _backtest_summaries()

    strategies: list[Strategy] = []
    for blueprint in _STRATEGY_BLUEPRINTS:
        own = [run for run in runs if run.strategy_id == blueprint["id"]]
        sharpes = [run.sharpe for run in own]
        returns = [run.total_return for run in own]

        strategies.append(
            Strategy(
                id=blueprint["id"],
                name=blueprint["name"],
                class_name=blueprint["class_name"],
                description=blueprint["description"],
                status=StrategyStatus.ACTIVE,
                tags=blueprint["tags"],
                universe=blueprint["universe"],
                parameters=[ParameterSpec(**spec) for spec in blueprint["parameters"]],
                run_count=len(own),
                best_sharpe=max(sharpes) if sharpes else None,
                best_return=max(returns) if returns else None,
                last_run_at=own[-1].created_at if own else None,
            )
        )
    return strategies


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------

_BACKTEST_SPECS: list[tuple[str, str, str, str, BacktestStatus]] = [
    ("bt-001", "Volume Momentum — 2025 H1", "portfolio_1", "AAPL", BacktestStatus.COMPLETED),
    ("bt-002", "Volume Momentum — tuned lookback", "portfolio_1", "NVDA", BacktestStatus.COMPLETED),
    ("bt-003", "Momentum baseline", "portfolio_2", "SPY", BacktestStatus.COMPLETED),
    ("bt-004", "Momentum — vol target 20%", "portfolio_2", "SPY", BacktestStatus.FAILED),
    ("bt-005", "Regime adaptive — full window", "portfolio_3", "SPY", BacktestStatus.COMPLETED),
    ("bt-006", "Regime adaptive — high VIX only", "portfolio_3", "TLT", BacktestStatus.RUNNING),
]

_STRATEGY_NAMES = {item["id"]: item["name"] for item in _STRATEGY_BLUEPRINTS}


def _equity_curve(salt: str, days: int, initial: float) -> list[EquityPoint]:
    """A random walk with mild upward drift, plus a benchmark on the same dates."""
    rng = _rng(f"equity:{salt}")
    equity = initial
    benchmark = initial
    points: list[EquityPoint] = []

    for offset in range(days):
        equity *= 1 + rng.gauss(0.0006, 0.011)
        benchmark *= 1 + rng.gauss(0.0004, 0.009)
        points.append(
            EquityPoint(
                date=(_EPOCH + timedelta(days=offset)).date().isoformat(),
                equity=round(equity, 2),
                benchmark=round(benchmark, 2),
            )
        )
    return points


def _trades(salt: str, symbol: str, count: int) -> list[Trade]:
    rng = _rng(f"trades:{salt}")
    trades: list[Trade] = []

    for index in range(count):
        entry = _EPOCH + timedelta(days=index * 7)
        exit_ = entry + timedelta(days=rng.randint(1, 6))
        entry_price = round(rng.uniform(80, 420), 2)
        return_pct = rng.gauss(0.008, 0.05)
        exit_price = round(entry_price * (1 + return_pct), 2)
        quantity = rng.randint(10, 200)

        trades.append(
            Trade(
                id=f"{salt}-t{index + 1:03d}",
                symbol=symbol,
                side="long" if rng.random() > 0.25 else "short",
                entry_date=entry.date().isoformat(),
                exit_date=exit_.date().isoformat(),
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=round((exit_price - entry_price) * quantity, 2),
                return_pct=round(return_pct, 5),
                fees=round(quantity * 0.005, 2),
            )
        )
    return trades


def _backtest_summaries() -> list[BacktestSummary]:
    summaries: list[BacktestSummary] = []

    for index, (run_id, name, strategy_id, symbol, status) in enumerate(_BACKTEST_SPECS):
        initial = 1_000_000.0
        curve = _equity_curve(run_id, 180, initial)
        final = curve[-1].equity

        peak = initial
        max_drawdown = 0.0
        for point in curve:
            peak = max(peak, point.equity)
            max_drawdown = min(max_drawdown, point.equity / peak - 1)

        rng = _rng(f"summary:{run_id}")
        summaries.append(
            BacktestSummary(
                id=run_id,
                name=name,
                strategy_id=strategy_id,
                strategy_name=_STRATEGY_NAMES[strategy_id],
                symbol=symbol,
                timeframe="1d",
                status=status,
                start_date=curve[0].date,
                end_date=curve[-1].date,
                created_at=_iso(_EPOCH + timedelta(days=180 + index)),
                initial_capital=initial,
                final_equity=final,
                total_return=round(final / initial - 1, 5),
                sharpe=round(rng.uniform(0.4, 2.1), 3),
                max_drawdown=round(max_drawdown, 5),
            )
        )
    return summaries


def list_backtests() -> list[BacktestSummary]:
    return _backtest_summaries()


def get_backtest(run_id: str) -> BacktestDetail | None:
    summary = next((item for item in _backtest_summaries() if item.id == run_id), None)
    if summary is None:
        return None

    curve = _equity_curve(run_id, 180, summary.initial_capital)
    trades = _trades(run_id, summary.symbol, 24)

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_win = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))

    return BacktestDetail(
        **summary.model_dump(),
        metrics=PerformanceMetrics(
            total_return=summary.total_return,
            cagr=round((1 + summary.total_return) ** (365 / 180) - 1, 5),
            sharpe=summary.sharpe,
            sortino=round(summary.sharpe * 1.35, 3),
            max_drawdown=summary.max_drawdown,
            volatility=0.174,
            win_rate=round(len(wins) / len(trades), 4) if trades else 0.0,
            # Guard the divide: a run with no losing trade would otherwise 500.
            profit_factor=round(gross_win / gross_loss, 3) if gross_loss else 0.0,
            total_trades=len(trades),
        ),
        equity_curve=curve,
        trades=trades,
        parameters={"lookback_days": 90, "top_n": 4, "slippage": 0.000001},
    )


# ---------------------------------------------------------------------------
# Live portfolios
# ---------------------------------------------------------------------------

_PORTFOLIO_SPECS: list[tuple[str, str, str, EngineState, list[str], float]] = [
    ("portfolio_1", "Volume Momentum", "VolumeMomentumStrategy", EngineState.RUNNING,
     ["AAPL", "MSFT", "NVDA", "AMZN"], 0.4),
    ("portfolio_2", "Cross-Sectional Momentum", "MomentumStrategy", EngineState.RUNNING,
     ["SPY", "TLT", "GLD", "XOM"], 0.35),
    ("portfolio_3", "Regime Adaptive", "RegimeAdaptiveStrategy", EngineState.STOPPED,
     ["SPY", "TLT", "GLD"], 0.25),
]


def _portfolio_summary(spec: tuple) -> PortfolioSummary:
    portfolio_id, name, strategy_class, state, tickers, weight = spec
    rng = _rng(f"portfolio:{portfolio_id}")

    starting = 1_000_000.0 * weight
    total_pnl = round(starting * rng.uniform(-0.05, 0.22), 2)
    total_value = round(starting + total_pnl, 2)
    cash = round(total_value * rng.uniform(0.05, 0.3), 2)

    return PortfolioSummary(
        id=portfolio_id,
        name=name,
        strategy_class=strategy_class,
        state=state,
        tickers=tickers,
        allocation_weight=weight,
        total_value=total_value,
        cash=cash,
        day_pnl=round(total_value * rng.uniform(-0.01, 0.012), 2),
        total_pnl=total_pnl,
        total_return=round(total_pnl / starting, 5),
        last_tick_at=_iso(_STARTED_AT) if state == EngineState.RUNNING else None,
    )


def list_portfolios() -> list[PortfolioSummary]:
    return [_portfolio_summary(spec) for spec in _PORTFOLIO_SPECS]


def get_portfolio(portfolio_id: str) -> PortfolioDetail | None:
    spec = next((item for item in _PORTFOLIO_SPECS if item[0] == portfolio_id), None)
    if spec is None:
        return None

    summary = _portfolio_summary(spec)
    tickers = summary.tickers
    rng = _rng(f"positions:{portfolio_id}")

    deployed = summary.total_value - summary.cash
    positions: list[Position] = []
    for ticker in tickers:
        weight = 1 / len(tickers)
        market_value = round(deployed * weight, 2)
        last_price = round(rng.uniform(60, 480), 2)
        quantity = round(market_value / last_price, 4)
        avg_price = round(last_price * rng.uniform(0.85, 1.1), 2)

        positions.append(
            Position(
                ticker=ticker,
                quantity=quantity,
                avg_price=avg_price,
                last_price=last_price,
                market_value=market_value,
                unrealized_pnl=round((last_price - avg_price) * quantity, 2),
                weight=round(market_value / summary.total_value, 5),
            )
        )

    return PortfolioDetail(
        **summary.model_dump(),
        config=PortfolioConfig(
            PORTFOLIO_ID=portfolio_id,
            TICKERS=tickers,
            INTERVAL=60,
            LOOKBACK_DAYS=90,
            EXCH="NASDAQ",
            WEIGHTS={ticker: round(1 / len(tickers), 4) for ticker in tickers},
            DATA_FEEDS=["market_data"],
            OMS=OmsConfig(
                enabled=True,
                default_algo="TWAP",
                duration_minutes=30,
                twap_num_slices=6,
                vwap_bucket_minutes=5,
                vwap_lookback_days=20,
                min_order_notional=500.0,
                fallback_to_market=True,
            ),
        ),
        positions=positions,
        started_at=_iso(_EPOCH),
        starting_capital=round(summary.total_value - summary.total_pnl, 2),
        consecutive_failures=0 if summary.state == EngineState.RUNNING else 2,
    )


def portfolio_equity(portfolio_id: str, days: int) -> EquitySeries:
    rng = _rng(f"portfolio-equity:{portfolio_id}")
    equity = 1_000_000.0
    start = datetime.now(timezone.utc) - timedelta(days=days)

    points = []
    for offset in range(days):
        equity *= 1 + rng.gauss(0.0005, 0.009)
        points.append(
            EquitySamplePoint(
                date=(start + timedelta(days=offset)).date().isoformat(),
                equity=round(equity, 2),
            )
        )
    return EquitySeries(points=points, downsampled=False)


def portfolio_composition(portfolio_id: str, days: int) -> CompositionSeries | None:
    detail = get_portfolio(portfolio_id)
    if detail is None:
        return None

    rng = _rng(f"composition:{portfolio_id}")
    start = datetime.now(timezone.utc) - timedelta(days=days)
    timestamps = [(start + timedelta(days=offset)).date().isoformat() for offset in range(days)]

    holdings: dict[str, list[float]] = {}
    for position in detail.positions:
        value = position.market_value
        series = []
        for _ in timestamps:
            value *= 1 + rng.gauss(0.0005, 0.012)
            series.append(round(value, 2))
        holdings[position.ticker] = series

    cash = [round(detail.cash * (1 + rng.gauss(0, 0.004)), 2) for _ in timestamps]

    return CompositionSeries(
        timestamps=timestamps, cash=cash, holdings=holdings, downsampled=False
    )


def portfolio_executions(portfolio_id: str) -> list[Execution] | None:
    spec = next((item for item in _PORTFOLIO_SPECS if item[0] == portfolio_id), None)
    if spec is None:
        return None

    tickers = spec[4]
    rng = _rng(f"executions:{portfolio_id}")
    now = datetime.now(timezone.utc)

    executions: list[Execution] = []
    for index in range(60):
        ticker = tickers[index % len(tickers)]
        price = round(rng.uniform(60, 480), 2)
        quantity = round(rng.uniform(1, 120), 2)
        side = "BUY" if rng.random() > 0.45 else "SELL"
        uses_oms = rng.random() > 0.3

        executions.append(
            Execution(
                id=f"{portfolio_id}-x{index + 1:04d}",
                ticker=ticker,
                side=side,
                quantity=quantity,
                price=price,
                notional=round(price * quantity, 2),
                executed_at=_iso(now - timedelta(minutes=index * 17)),
                algo="TWAP" if uses_oms else None,
                parent_order_id=f"{portfolio_id}-p{index // 3 + 1:04d}" if uses_oms else None,
            )
        )
    return executions


def portfolio_correlations(portfolio_id: str) -> CorrelationMatrix | None:
    spec = next((item for item in _PORTFOLIO_SPECS if item[0] == portfolio_id), None)
    if spec is None:
        return None

    tickers = spec[4]
    rng = _rng(f"correlations:{portfolio_id}")
    size = len(tickers)

    # Build the upper triangle then mirror it, so the matrix is symmetric with a
    # unit diagonal — a correlation matrix that fails those two properties would
    # be visibly wrong in the heatmap.
    matrix = [[1.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(row + 1, size):
            value = round(rng.uniform(-0.4, 0.9), 4)
            matrix[row][column] = value
            matrix[column][row] = value

    return CorrelationMatrix(tickers=tickers, matrix=matrix, lookback_days=90)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

_SERVICES = [
    ("RunEngine", "Trading engine"),
    ("realtimeDataIngestor", "Market data ingestor"),
    ("nlp-daemon", "NLP sentiment daemon"),
    ("risk_manager", "Risk manager"),
]


def system_status() -> SystemStatus:
    now = datetime.now(timezone.utc)
    services = [
        Service(
            name=name,
            label=label,
            state=ServiceState.UP,
            detail=None,
            last_heartbeat_at=_iso(now),
        )
        for name, label in _SERVICES
    ]

    # Headline is the worst state across services, not a separate flag that can
    # disagree with the list beneath it.
    worst = ServiceState.UP
    for service in services:
        if service.state == ServiceState.DOWN:
            worst = ServiceState.DOWN
            break
        if service.state == ServiceState.DEGRADED:
            worst = ServiceState.DEGRADED

    return SystemStatus(
        state=worst,
        market_open=now.weekday() < 5 and 13 <= now.hour < 20,
        services=services,
        version="0.1.0",
        uptime_seconds=round((now - _STARTED_AT).total_seconds(), 1),
        checked_at=_iso(now),
    )


_LOG_MESSAGES = [
    (LogLevel.INFO, "RunEngine", "Portfolio thread started", None),
    (LogLevel.INFO, "VolumeMomentum_1", "OnData complete, 4 signals evaluated", "portfolio_1"),
    (LogLevel.DEBUG, "MQSDBConnector", "Atomic state query returned in 12ms", None),
    (LogLevel.INFO, "OrderManager", "Child order filled: 40 @ 187.22", "portfolio_1"),
    (LogLevel.WARNING, "fmpMarketData", "Rate limit at 80%, backing off", None),
    (LogLevel.INFO, "Momentum_2", "Rebalance skipped, weights within tolerance", "portfolio_2"),
    (LogLevel.ERROR, "RegimeAdaptive_3", "VIX feed stale, holding last regime", "portfolio_3"),
    (LogLevel.INFO, "risk_manager", "Daily allocation unchanged", None),
]


def log_tail(size: int) -> list[LogEntry]:
    now = datetime.now(timezone.utc)
    entries: list[LogEntry] = []

    for index in range(size):
        level, logger, message, portfolio_id = _LOG_MESSAGES[index % len(_LOG_MESSAGES)]
        entries.append(
            LogEntry(
                id=f"log-{index + 1:05d}",
                timestamp=_iso(now - timedelta(seconds=index * 11)),
                level=level,
                logger=logger,
                message=message,
                portfolio_id=portfolio_id,
            )
        )
    # Oldest first, so the viewer can append new lines at the bottom.
    return list(reversed(entries))
