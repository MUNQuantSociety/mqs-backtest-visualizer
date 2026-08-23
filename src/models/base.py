"""Declarative base and the schema every app-owned table lives in.

The connection is to the production MQS trading database, which already owns
``public`` (market data, live trading books). Everything this application
creates is namespaced under ``app`` so the two never collide and a
``DROP SCHEMA app CASCADE`` can never take the trading system with it.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

# Referenced by models, ``src/db/init.py``, and the seed script — declared once
# so a rename cannot half-apply.
APP_SCHEMA = "app"


class Base(DeclarativeBase):
    """Shared declarative base for every ``app.*`` table."""
