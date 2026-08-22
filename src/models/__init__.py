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
    "Base",
    "BacktestRun",
    "RunEquityPoint",
    "RunMetrics",
    "RunTrade",
    "RUN_PURPOSES",
    "RUN_STATUSES",
    "Strategy",
    "STRATEGY_KINDS",
    "STRATEGY_STATUSES",
]
