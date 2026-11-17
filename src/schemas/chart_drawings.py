"""Chart drawings: what the Build page's Draw menu saves for each ticker.

Mirrors ``src/features/market/components/chart-drawings-model.ts``. A point
is a session date and a price, never a pixel, so a drawing lands on the same
candles at any zoom or range. The checks here are the ones the chart relies
on to draw a saved list without guarding every field again.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from src.schemas.common import CamelModel

DrawingTool = Literal["trend", "ray", "horizontal", "vertical", "rectangle", "fib", "text"]

# How many points each tool is placed with.
POINTS_PER_TOOL: dict[str, int] = {
    "trend": 2,
    "ray": 2,
    "horizontal": 1,
    "vertical": 1,
    "rectangle": 2,
    "fib": 2,
    "text": 1,
}

# Generous for a chart, small enough that one row stays a few kilobytes.
MAX_DRAWINGS = 200
MAX_NOTE_LENGTH = 200


class DrawingPoint(CamelModel):
    date: str
    price: float

    @field_validator("date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("Dates are YYYY-MM-DD.") from None
        if len(value) != 10:
            raise ValueError("Dates are YYYY-MM-DD.")
        return value

    @field_validator("price")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("A price must be a finite number.")
        return value


class ChartDrawing(CamelModel):
    """One drawing. ``text`` is a note's words and is set only on a note."""

    id: str = Field(min_length=1, max_length=64)
    tool: DrawingTool
    points: list[DrawingPoint]
    text: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)

    @model_validator(mode="after")
    def _shape_fits_tool(self) -> ChartDrawing:
        wanted = POINTS_PER_TOOL[self.tool]
        if len(self.points) != wanted:
            raise ValueError(f"A {self.tool} drawing has {wanted} point(s), not {len(self.points)}.")
        if self.tool == "text":
            if not (self.text or "").strip():
                raise ValueError("A text note needs some text.")
        elif self.text is not None:
            raise ValueError("Only a text note carries text.")
        return self


class ChartDrawingsUpdate(CamelModel):
    """The whole list for a ticker; it replaces what was saved before."""

    drawings: list[ChartDrawing] = Field(max_length=MAX_DRAWINGS)

    @model_validator(mode="after")
    def _unique_ids(self) -> ChartDrawingsUpdate:
        ids = [drawing.id for drawing in self.drawings]
        if len(ids) != len(set(ids)):
            raise ValueError("Each drawing needs its own id.")
        return self


class ChartDrawingsResponse(CamelModel):
    """A member's drawings for ``ticker``; an empty list when there are none.

    ``updated_at`` is null when nothing has been saved for the ticker.
    """

    ticker: str
    drawings: list[ChartDrawing]
    updated_at: str | None = None
