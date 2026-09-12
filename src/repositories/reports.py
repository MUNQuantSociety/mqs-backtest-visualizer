"""One immutable JSON document per successful, user-owned backtest."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import Engine, delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import BacktestReport
from src.schemas.backtests import BacktestDetail, BacktestStatus, BacktestSummary

REPORT_VERSION = 1


def document(detail: BacktestDetail) -> dict:
    if detail.status != BacktestStatus.COMPLETED:
        raise ValueError("Only successful backtests can be saved.")
    if not detail.equity_curve:
        raise ValueError("A completed report must contain equity observations.")
    result = detail.model_dump(mode="json", by_alias=True, exclude={"status", "progress_pct", "error_message"})
    json.dumps(result, allow_nan=False)
    return result


def save(engine: Engine, owner_id: uuid.UUID, detail: BacktestDetail) -> None:
    if owner_id is None:
        raise ValueError("A saved report requires an owner.")
    payload = document(detail)
    with engine.begin() as connection:
        connection.execute(insert(BacktestReport).values(
            id=uuid.UUID(detail.id), owner_id=owner_id,
            created_at=datetime.fromisoformat(detail.created_at.replace("Z", "+00:00")),
            strategy_key=detail.strategy_id, name=detail.name,
            version=REPORT_VERSION, results=payload,
        ))


def to_detail(report: BacktestReport) -> BacktestDetail:
    if report.version != REPORT_VERSION:
        raise ValueError(f"Unsupported saved report version: {report.version}")
    return BacktestDetail.model_validate({
        **report.results, "id": str(report.id), "name": report.name,
        "strategyId": report.strategy_key, "createdAt": report.created_at.isoformat().replace("+00:00", "Z"),
        # Compatibility with the existing frontend; this is not stored.
        "status": "completed", "progressPct": 100, "errorMessage": None,
    })


async def get(session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID) -> BacktestReport | None:
    return (await session.execute(select(BacktestReport).where(
        BacktestReport.id == run_id, BacktestReport.owner_id == owner_id,
    ))).scalar_one_or_none()


async def remove(session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
    result = await session.execute(delete(BacktestReport).where(
        BacktestReport.id == run_id, BacktestReport.owner_id == owner_id,
    ))
    return bool(result.rowcount)


async def list_reports(session: AsyncSession, owner_id: uuid.UUID, *, search=None, strategy_key=None, page=1, page_size=25):
    report = BacktestReport
    predicates = [report.owner_id == owner_id,
                  func.coalesce(report.results["reportMetadata"]["purpose"].astext, "user") == "user"]
    if strategy_key:
        predicates.append(report.strategy_key == strategy_key)
    if search:
        needle = f"%{search.lower()}%"
        predicates.append(or_(func.lower(report.name).like(needle),
                              func.lower(report.strategy_key).like(needle),
                              func.lower(report.results["strategyName"].astext).like(needle),
                              func.lower(report.results["symbol"].astext).like(needle),
                              func.lower(report.results["parameters"]["universe"].astext).like(needle)))
    total = (await session.execute(select(func.count()).select_from(report).where(*predicates))).scalar_one()
    # Project just the card fields in SQL; do not transfer every curve/trade list.
    columns = [report.id, report.name, report.strategy_key, report.created_at]
    fields = ("strategyName", "symbol", "timeframe", "startDate", "endDate", "initialCapital", "finalEquity", "totalReturn", "sharpe", "maxDrawdown")
    columns.extend(report.results[field].label(field) for field in fields)
    rows = (await session.execute(select(*columns).where(*predicates)
            .order_by(report.created_at.desc(), report.id).offset((page - 1) * page_size).limit(page_size))).mappings()
    items = [BacktestSummary.model_validate({**dict(row), "id": str(row["id"]),
             "strategyId": row["strategy_key"], "createdAt": row["created_at"].isoformat(),
             "status": "completed"}) for row in rows]
    return items, int(total)
