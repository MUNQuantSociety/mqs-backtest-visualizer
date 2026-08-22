"""Backtest runs and everything one run produces.

Four tables rather than one JSON blob: the equity curve and the trade list are
each thousands of rows the client pages and filters, and the headline numbers
are duplicated onto the run row so the list endpoint never has to join.
"""

from __future__ import annotations

import uuid
# ``date`` is aliased: RunEquityPoint/RunTrade have columns literally named
# ``date``, and an unaliased import would shadow the type in Mapped[...].
from datetime import date as DateType
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import APP_SCHEMA, Base

RUN_STATUSES = ("queued", "running", "completed", "failed")
RUN_PURPOSES = ("user", "validation")

# Money and ratios are NUMERIC, never FLOAT: equity is currency and a
# half-cent of binary rounding drift compounds visibly across a curve.
_MONEY = Numeric(20, 6)
_RATIO = Numeric(20, 10)


class BacktestRun(Base):
    """One execution of one strategy over one window."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        # Newest-first is the list endpoint's default ordering; status is its
        # only filter that is not already a primary key lookup.
        Index("ix_backtest_runs_created_at_desc", text("created_at DESC")),
        Index("ix_backtest_runs_status", "status"),
        {"schema": APP_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_key: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{APP_SCHEMA}.strategies.key", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")

    # The submitted parameter overlay, echoed back to the client as
    # `parameters` and merged over the strategy's config.json at run time.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    start_date: Mapped[DateType] = mapped_column(Date, nullable=False)
    end_date: Mapped[DateType] = mapped_column(Date, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False, default="1d")
    # The literal string "MULTI" when the universe has more than one ticker —
    # the frontend's row shape has a single symbol field and the ticker list
    # rides in `params`.
    symbol: Mapped[str] = mapped_column(Text, nullable=False)

    initial_capital: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    # Null until the run finishes. Denormalised from run_metrics so the list
    # endpoint is a single-table scan.
    final_equity: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    total_return: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    sharpe: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    max_drawdown: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)

    progress_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")

    # Auth lands in a parallel session; the column and the repository filter
    # seam exist now so that work is a filter change, not a migration.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # A validation run for an uploaded strategy goes through the identical
    # pipeline; this is the only thing that tells the two apart.
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    # Cooperative cancellation: the worker polls this column, there is no
    # signal to send across a process pool boundary.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    metrics: Mapped["RunMetrics | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    equity_points: Mapped[list["RunEquityPoint"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunEquityPoint.seq",
    )
    trades: Mapped[list["RunTrade"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunTrade.seq"
    )


class RunMetrics(Base):
    """The headline performance numbers, one row per run.

    Column names are the frontend's ``PerformanceMetrics`` fields in snake_case,
    so the serialisation layer is a rename and nothing more.
    """

    __tablename__ = "run_metrics"
    __table_args__ = {"schema": APP_SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.backtest_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    total_return: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    cagr: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    sharpe: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    sortino: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    max_drawdown: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    volatility: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    win_rate: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    profit_factor: Mapped[Decimal | None] = mapped_column(_RATIO, nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Anything the engine's reporting produces that the frontend does not read
    # yet — kept rather than dropped so a new chart needs no re-run.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    run: Mapped[BacktestRun] = relationship(back_populates="metrics")


class RunEquityPoint(Base):
    """One point on a run's equity curve. Downsampled to daily before insert."""

    __tablename__ = "run_equity_points"
    __table_args__ = {"schema": APP_SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.backtest_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Explicit sequence rather than relying on date: it survives duplicate
    # dates and preserves insertion order without an ORDER BY on a text cast.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[DateType] = mapped_column(Date, nullable=False)
    equity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    benchmark: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)

    run: Mapped[BacktestRun] = relationship(back_populates="equity_points")


class RunTrade(Base):
    """One round trip — an entry paired with the fill that closed it.

    The engine emits single-leg fills; the pairing service turns them into
    these rows, which is the shape the trade table and P&L histogram consume.
    """

    __tablename__ = "run_trades"
    __table_args__ = {"schema": APP_SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.backtest_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    entry_date: Mapped[DateType] = mapped_column(Date, nullable=False)
    # Null while the lot is still open at the end of the window.
    exit_date: Mapped[DateType | None] = mapped_column(Date, nullable=True)
    entry_price: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    pnl: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0)
    return_pct: Mapped[Decimal] = mapped_column(_RATIO, nullable=False, default=0)
    fees: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0)

    run: Mapped[BacktestRun] = relationship(back_populates="trades")
