"""SQLAlchemy models for the ``app`` schema.

Importing this package registers every table on ``Base.metadata``, which is
what ``src/db/init.py`` and the seed scripts rely on — a model that is never
imported is a table that never gets created.
"""

from src.models.base import APP_SCHEMA, Base
from src.models.runs import (
    RUN_PURPOSES,
    RUN_STATUSES,
    BacktestRun,
    RunEquityPoint,
    RunMetrics,
    RunTrade,
)
from src.models.strategies import STRATEGY_KINDS, STRATEGY_STATUSES, Strategy

__all__ = [
    "APP_SCHEMA",
    "RUN_PURPOSES",
    "RUN_STATUSES",
    "STRATEGY_KINDS",
    "STRATEGY_STATUSES",
    "BacktestRun",
    "Base",
    "RunEquityPoint",
    "RunMetrics",
    "RunTrade",
    "Strategy",
]
