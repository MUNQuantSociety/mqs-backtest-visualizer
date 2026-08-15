"""Shared response primitives.

The frontend parses every payload with Zod and its schemas are camelCase, so
models here serialise to camelCase while staying snake_case in Python. Mismatch
is not a cosmetic problem: an unexpected key name surfaces as a validation error
in the browser, not a missing value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for every response model. Serialises snake_case fields as camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Page(CamelModel):
    """Pagination envelope shared by the list endpoints.

    ``total`` is the count before slicing, so the client can render "showing 25
    of 340" without a second request.
    """

    total: int
    page: int
    page_size: int


def paginate(items: list, page: int, page_size: int) -> tuple[list, int]:
    """Slice ``items`` for a 1-indexed page. Returns the slice and the full count."""
    total = len(items)
    start = max(page - 1, 0) * page_size
    return items[start : start + page_size], total
