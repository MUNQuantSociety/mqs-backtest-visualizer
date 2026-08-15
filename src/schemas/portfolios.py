"""Live portfolio response models.

Mirrors ``src/features/portfolios/types.ts``. Two naming conventions coexist on
purpose: the transport envelope is camelCase like every other endpoint, while
``config`` keeps MQSMaster's own SCREAMING_SNAKE keys verbatim because
``BasePortfolio`` reads ``TICKERS`` and ``LOOKBACK_DAYS`` by name. Renaming them
in transit would need a bidirectional mapping table on both sides.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.schemas.common import CamelModel, Page


class EngineState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    HALTED = "halted"


class OmsConfig(BaseModel):
    """OMS block — gated per portfolio by ``OMS.enabled`` in ``config.json``."""

    enabled: bool
    default_algo: Literal["TWAP", "VWAP", "MARKET"]
    duration_minutes: int
    twap_num_slices: int
    vwap_bucket_minutes: int
    vwap_lookback_days: int
    min_order_notional: float
    fallback_to_market: bool


class PortfolioConfig(BaseModel):
    """Mirrors ``src/portfolios/portfolio_<n>/config.json``.

    ``extra="allow"`` is load-bearing: portfolios 4-8 carry extra blocks
    (``RBP_CONFIG``, ``PORTFOLIO_6_CONFIG``, ``asset_groups``) that no shared
    schema can enumerate. Dropping them would make the config viewer silently
    lie about what the engine is running.
    """

    model_config = ConfigDict(extra="allow")

    PORTFOLIO_ID: str
    TICKERS: list[str]
    INTERVAL: int
    LOOKBACK_DAYS: int
    WEIGHTS: dict[str, float]
    DATA_FEEDS: list[str]
    EXCH: str | None = None
    OMS: OmsConfig | None = None


class Position(CamelModel):
    ticker: str
    quantity: float
    avg_price: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    weight: float


class Execution(CamelModel):
    """A single fill from ``trade_execution_logs``.

    Distinct from the backtests feature's ``Trade``, which is a round trip: the
    live system logs fills as they happen and does not yet know whether a
    position will ever be closed.
    """

    id: str
    ticker: str
    side: Literal["BUY", "SELL"]
    quantity: float
    price: float
    notional: float
    executed_at: str
    algo: Literal["TWAP", "VWAP", "MARKET"] | None = None
    parent_order_id: str | None = None


class EquitySamplePoint(CamelModel):
    date: str
    equity: float


class EquitySeries(CamelModel):
    points: list[EquitySamplePoint]
    # True when the server reduced resolution. The UI says so rather than
    # implying full fidelity.
    downsampled: bool = False


class CompositionSeries(CamelModel):
    """Per-component notional over time — cash plus one entry per ticker.

    Stored column-wise rather than as a row of objects per timestamp: at minute
    resolution over a year that is ~98k points per series, and the row shape
    would repeat every key 98k times.
    """

    timestamps: list[str]
    cash: list[float]
    holdings: dict[str, list[float]]
    downsampled: bool = False


class PortfolioSummary(CamelModel):
    id: str
    name: str
    strategy_class: str
    state: EngineState
    tickers: list[str]
    allocation_weight: float
    total_value: float
    cash: float
    day_pnl: float
    total_pnl: float
    total_return: float
    last_tick_at: str | None = None


class PortfolioDetail(PortfolioSummary):
    config: PortfolioConfig
    positions: list[Position]
    started_at: str
    starting_capital: float
    consecutive_failures: int = 0


class PortfolioListResponse(Page):
    items: list[PortfolioSummary]


class ExecutionListResponse(Page):
    items: list[Execution]


class CorrelationMatrix(CamelModel):
    """Pairwise return correlations.

    ``matrix[i][j]`` is ``tickers[i]`` against ``tickers[j]``. The full square is
    returned rather than a triangle so the client never mirrors indices.
    """

    tickers: list[str]
    matrix: list[list[float]]
    lookback_days: int
