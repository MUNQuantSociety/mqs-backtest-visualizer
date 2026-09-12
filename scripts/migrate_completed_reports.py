"""Copy complete legacy reports into the JSON table without changing old data.

Dry run by default. --assign-unowned-to is an explicit ownership mapping for
legacy rows; without it unowned rows are skipped. Re-running is idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, selectinload

from src.db.engine import create_sync_engine
from src.db.init import init_database
from src.models import BacktestReport, BacktestRun, Strategy
from src.repositories.reports import REPORT_VERSION, document
from src.repositories.runs import RunListRow
from src.services.backtests import to_detail


def migrate(engine, *, apply=False, assign_unowned_to=None):
    if not inspect(engine).has_table("backtest_runs", schema="app"):
        return {"eligible": 0, "copied": 0, "already_saved": 0, "unowned": 0}
    destination_exists = inspect(engine).has_table("backtest_reports", schema="app")
    stats = {"eligible": 0, "copied": 0, "already_saved": 0, "unowned": 0}
    pending = []
    with Session(engine) as session:
        if assign_unowned_to is not None:
            from sqlalchemy import text
            exists = session.execute(text("SELECT 1 FROM public.user_creds WHERE id = :id"),
                                     {"id": assign_unowned_to}).scalar_one_or_none()
            if exists is None:
                raise ValueError("The requested legacy owner does not exist in public.user_creds.")
        statement = select(BacktestRun, Strategy.name).join(Strategy, Strategy.key == BacktestRun.strategy_key)
        statement = statement.where(BacktestRun.status == "completed").options(
            selectinload(BacktestRun.metrics), selectinload(BacktestRun.equity_points),
            selectinload(BacktestRun.trades))
        for run, name in session.execute(statement):
            owner = run.owner_id or assign_unowned_to
            if owner is None:
                stats["unowned"] += 1
                continue
            detail = to_detail(RunListRow(run, name))
            detail.report_metadata.update(purpose=run.purpose, engineVersion=run.engine_version)
            payload = document(detail)
            existing = session.get(BacktestReport, run.id) if destination_exists else None
            if existing is not None:
                if existing.owner_id != owner or existing.results != payload:
                    raise ValueError(f"Conflicting saved report {run.id}; nothing will be overwritten.")
                stats["already_saved"] += 1
                continue
            pending.append(dict(id=run.id, owner_id=owner, created_at=run.created_at,
                strategy_key=run.strategy_key, name=run.name, version=REPORT_VERSION, results=payload))
        stats["eligible"] = len(pending)
    if apply:
        init_database(engine)
        with Session(engine) as session, session.begin():
            for values in pending:
                session.add(BacktestReport(**values))
        stats["copied"] = len(pending)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--assign-unowned-to", type=uuid.UUID)
    args = parser.parse_args()
    engine = create_sync_engine()
    try:
        print(json.dumps(migrate(engine, apply=args.apply, assign_unowned_to=args.assign_unowned_to)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
