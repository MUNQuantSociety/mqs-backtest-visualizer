"""The strategy registry — one row per thing a student can backtest.

Built-in strategies (the vendored MQSMaster portfolios) and user uploads share
this table deliberately: a storage_key selects a stored package for either
kind; unpublished built-ins can still run in local mode. That is why there is no
separate "drafts" table — an upload's whole lifecycle is the ``status`` column.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import APP_SCHEMA, Base

# Registry vocabularies. Kept as plain strings rather than PostgreSQL enums:
# adding a status must not require a migration on a schema this young.
STRATEGY_KINDS = ("builtin", "user")
STRATEGY_STATUSES = ("active", "validating", "failed_validation", "archived")


class Strategy(Base):
    """A backtestable strategy, built in or uploaded."""

    __tablename__ = "strategies"
    __table_args__ = {"schema": APP_SCHEMA}

    # The key is the public identifier the frontend sends back as `strategyId`
    # ("portfolio_1"), so it is the primary key rather than a surrogate UUID.
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    universe: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # List of ParameterSpec dicts (key/label/type/default/min/max) — stored in
    # the frontend's shape so the catalogue endpoint is a passthrough and the
    # run endpoint validates submitted params against the same document.
    param_specs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False, default="builtin")
    # Import path for built-ins ("engine.strategies.portfolio_1.strategy.VolMomentum").
    class_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stored package for uploads or published built-ins ("strategies/<key>/").
    # Takes precedence over class_path, retained as builtin metadata/rollback.
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # How this strategy was authored, when it was authored as a fragment:
    # ``{"body": str, "indicators": [...], "state": {...}}``.
    #
    # On the row rather than in the strategy store, deliberately. The store has
    # a two-file contract (``strategy.py`` + ``config.json``) and its retry path
    # in ``packaging.store_strategy_source`` compares those two names exactly,
    # returning early on a match — a third file would be silently skipped on any
    # retry, with nothing raised. The row is also where this belongs anyway: it
    # is metadata about how the source was produced, not part of the package the
    # engine loads.
    #
    # NULL means "authored as a whole file", which is every row that exists
    # today and every upload through ``POST /strategies``. No backfill: the
    # editor reads the assembled ``strategy.py`` for those, which is exactly
    # what a member wrote.
    authoring: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # The run that proved an uploaded strategy works. ``use_alter`` because
    # strategies and backtest_runs reference each other; without it create_all
    # cannot order the CREATE TABLE statements.
    validation_run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            f"{APP_SCHEMA}.backtest_runs.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_strategies_validation_run_id",
        ),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Staging area for uploaded source between task 2 (persist the row) and
    # task 9 (the store becomes the system of record). Task 9 migrates off it.
    source_staging: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
