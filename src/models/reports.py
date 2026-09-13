"""Successful backtests only. Execution state never belongs in this table."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import APP_SCHEMA, Base


class BacktestReport(Base):
    __tablename__ = "backtest_reports"
    __table_args__ = (
        Index("ix_backtest_reports_owner_created", "owner_id", text("created_at DESC"), "id"),
        CheckConstraint("version > 0", name="ck_report_version"),
        CheckConstraint("jsonb_typeof(results) = 'object' AND NOT (results ? 'status')", name="ck_report_document"),
        {"schema": APP_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # New owners are verified app.users identities. No FK or ID rewrite:
    # completed reports retain legacy owners until an explicit migration.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    strategy_key: Mapped[str] = mapped_column(Text, ForeignKey(f"{APP_SCHEMA}.strategies.key", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
