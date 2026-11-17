"""Drawings a member has made on a ticker's chart on the Build page."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import APP_SCHEMA, Base


class ChartDrawingSet(Base):
    """One member's drawings for one ticker, kept whole.

    The chart saves the full list each time it changes, so a row is replaced
    rather than edited drawing by drawing. ``drawings`` is the list the API
    validated (``src/schemas/chart_drawings.py``); nothing else writes it.
    """

    __tablename__ = "chart_drawings"
    __table_args__ = {"schema": APP_SCHEMA}

    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    drawings: Mapped[list] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
