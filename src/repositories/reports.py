"""One immutable JSON document per successful, user-owned backtest."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import Engine, delete, func, insert, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import BacktestReport
from src.models.base import APP_SCHEMA
from src.schemas.backtests import BacktestDetail, BacktestStatus, BacktestSummary

REPORT_VERSION = 1
EXAMPLE_WARNING = (
    "Simulated onboarding example. Prices, trades, and returns are generated "
    "illustrations; no FMP market data was fetched and no backtest engine was "
    "executed. Excluded from real backtest history and performance totals."
)


def example_name(strategy_name: str) -> str:
    """Keep the simulation disclosure visible in existing list/detail clients."""
    return f"Example: {strategy_name} (simulated)"


def _is_example_metadata(metadata: dict) -> bool:
    starter = metadata.get("starterExample")
    market_data = metadata.get("marketData")
    return (
        metadata.get("purpose") == "example"
        or (isinstance(starter, dict) and starter.get("generated") is True)
        or (isinstance(market_data, dict)
            and market_data.get("source") == "bundled_starter_example")
    )


def document(detail: BacktestDetail) -> dict:
    if detail.status != BacktestStatus.COMPLETED:
        raise ValueError("Only successful backtests can be saved.")
    if not detail.equity_curve:
        raise ValueError("A completed report must contain equity observations.")
    result = detail.model_dump(mode="json", by_alias=True, exclude={"status", "progress_pct", "error_message"})
    json.dumps(result, allow_nan=False)
    return result


def _record_values(owner_id: uuid.UUID, detail: BacktestDetail) -> dict:
    return {
        "id": uuid.UUID(detail.id),
        "owner_id": owner_id,
        "created_at": datetime.fromisoformat(
            detail.created_at.replace("Z", "+00:00")
        ),
        "strategy_key": detail.strategy_id,
        "name": detail.name,
        "version": REPORT_VERSION,
        "results": document(detail),
    }


def save(engine: Engine, owner_id: uuid.UUID, detail: BacktestDetail) -> None:
    if owner_id is None:
        raise ValueError("A saved report requires an owner.")
    values = _record_values(owner_id, detail)
    with engine.begin() as connection:
        connection.execute(insert(BacktestReport).values(**values))


_RUN_TICKERS_SQL = text(
    "SELECT DISTINCT jsonb_array_elements_text(results -> 'parameters' -> 'universe') "
    f'FROM "{APP_SCHEMA}".backtest_reports '
    "WHERE jsonb_typeof(results -> 'parameters' -> 'universe') = 'array'"
)


async def run_tickers(session: AsyncSession) -> set[str]:
    """Every ticker any saved report has traded, across all owners.

    Shared on purpose: which symbols the club has backtested is not private
    the way the reports themselves are, and it is exactly the set a member
    is most likely to type next.
    """
    result = await session.execute(_RUN_TICKERS_SQL)
    return {str(row[0]).strip().upper() for row in result if row[0]}


async def add_completed_reports(
    session: AsyncSession,
    owner_id: uuid.UUID,
    details: list[BacktestDetail],
) -> None:
    """Stage ordinary completed reports in the caller's transaction."""
    if owner_id is None:
        raise ValueError("A saved report requires an owner.")
    session.add_all(
        [BacktestReport(**_record_values(owner_id, detail)) for detail in details]
    )
    await session.flush()


def to_detail(report: BacktestReport) -> BacktestDetail:
    if report.version != REPORT_VERSION:
        raise ValueError(f"Unsupported saved report version: {report.version}")
    metadata = dict(report.results.get("reportMetadata") or {})
    name = report.name
    if _is_example_metadata(metadata):
        # Older local examples used purpose=user. Disclose them consistently
        # without rewriting immutable stored reports or changing their IDs.
        metadata["purpose"] = "example"
        starter = metadata.get("starterExample")
        metadata["starterExample"] = {
            **(starter if isinstance(starter, dict) else {}),
            "generated": True, "deletable": True, "message": EXAMPLE_WARNING,
        }
        name = example_name(report.results["strategyName"])
    return BacktestDetail.model_validate({
        **report.results, "id": str(report.id), "name": name,
        "reportMetadata": metadata,
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


def _example_predicate():
    metadata = BacktestReport.results["reportMetadata"]
    return func.coalesce(or_(
        metadata["purpose"].astext == "example",
        metadata["starterExample"]["generated"].astext == "true",
        metadata["marketData"]["source"].astext == "bundled_starter_example",
    ), False)


def _visible_owner_predicates(owner_id: uuid.UUID, *, examples: bool = False) -> list:
    report = BacktestReport
    if examples:
        return [report.owner_id == owner_id, _example_predicate()]
    return [report.owner_id == owner_id, ~_example_predicate(),
            func.coalesce(report.results["reportMetadata"]["purpose"].astext, "user") == "user"]


async def owner_has_visible_reports(
    session: AsyncSession, owner_id: uuid.UUID
) -> bool:
    """Onboarding respects existing real reports and earlier examples alike."""
    statement = (
        select(BacktestReport.id)
        .where(BacktestReport.owner_id == owner_id, or_(
            func.coalesce(BacktestReport.results["reportMetadata"]["purpose"].astext, "user") == "user",
            _example_predicate(),
        ))
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


async def list_reports(session: AsyncSession, owner_id: uuid.UUID, *, search=None, strategy_key=None, page=1, page_size=25, examples=False):
    report = BacktestReport
    predicates = _visible_owner_predicates(owner_id, examples=examples)
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
    columns = [report.id, report.name, report.strategy_key, report.created_at,
               _example_predicate().label("is_example")]
    fields = ("strategyName", "symbol", "timeframe", "startDate", "endDate", "initialCapital", "finalEquity", "totalReturn", "sharpe", "maxDrawdown")
    columns.extend(report.results[field].label(field) for field in fields)
    rows = (await session.execute(select(*columns).where(*predicates)
            .order_by(report.created_at.desc(), report.id).offset((page - 1) * page_size).limit(page_size))).mappings()
    items = [BacktestSummary.model_validate({**dict(row), "id": str(row["id"]),
             "name": example_name(row["strategyName"]) if row.get("is_example") else row["name"],
             "strategyId": row["strategy_key"], "createdAt": row["created_at"].isoformat(),
             "status": "completed"}) for row in rows]
    return items, int(total)
