"""First-sign-in examples are ordinary owned reports and stay deleted."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.models import AppUser
from src.repositories import reports
from src.services import starter_reports


OWNER = uuid.UUID("00000000-0000-0000-0000-000000000101")
OTHER_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000102")


def _user(owner=OWNER):
    return AppUser(
        id=owner,
        issuer="https://issuer.example/pool",
        subject=f"subject-{owner}",
    )


def _strategy(key):
    return SimpleNamespace(
        key=key,
        name=(
            "Volatility Momentum"
            if key == "portfolio_1"
            else "Multi-Indicator Momentum"
        ),
        universe=["AAPL", "TSLA", "AMD", "MSFT", "NVDA"],
        enabled=True,
        kind="builtin",
        status="active",
    )


def test_new_owner_receives_two_complete_chart_reports(monkeypatch):
    user = _user()
    monkeypatch.setattr(
        starter_reports.users, "lock_user", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        starter_reports.reports,
        "owner_has_visible_reports",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        starter_reports.strategies,
        "get_strategy",
        AsyncMock(side_effect=lambda session, key: _strategy(key)),
    )
    add = AsyncMock()
    monkeypatch.setattr(starter_reports.reports, "add_completed_reports", add)

    created = asyncio.run(starter_reports.ensure_starter_reports(object(), user))

    assert created == 2
    assert user.starter_reports_seeded_at is not None
    details = add.await_args.args[2]
    assert [detail.strategy_id for detail in details] == ["portfolio_1", "portfolio_2"]
    assert [detail.name for detail in details] == [
        "Example: Volatility Momentum (simulated)",
        "Example: Multi-Indicator Momentum (simulated)",
    ]
    for detail in details:
        assert detail.status == "completed"
        assert len(detail.equity_curve) == starter_reports.STARTER_TRADING_DAYS
        assert len(detail.trades) == detail.metrics.total_trades == 6
        assert detail.report_metadata["starterExample"]["deletable"] is True
        assert detail.report_metadata["purpose"] == "example"
        assert "no FMP market data" in detail.report_metadata["starterExample"]["message"]
        assert "no backtest engine" in detail.report_metadata["starterExample"]["message"]
        json.dumps(reports.document(detail), allow_nan=False)


def test_existing_history_is_not_cluttered(monkeypatch):
    user = _user()
    monkeypatch.setattr(
        starter_reports.users, "lock_user", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        starter_reports.reports,
        "owner_has_visible_reports",
        AsyncMock(return_value=True),
    )
    lookup = AsyncMock(side_effect=AssertionError("strategies are not needed"))
    add = AsyncMock(side_effect=AssertionError("reports must not be added"))
    monkeypatch.setattr(starter_reports.strategies, "get_strategy", lookup)
    monkeypatch.setattr(starter_reports.reports, "add_completed_reports", add)

    assert asyncio.run(starter_reports.ensure_starter_reports(object(), user)) == 0
    assert user.starter_reports_seeded_at is not None
    lookup.assert_not_awaited()
    add.assert_not_awaited()


def test_deleted_examples_are_not_recreated(monkeypatch):
    user = _user()
    user.starter_reports_seeded_at = datetime.now(timezone.utc)
    lock = AsyncMock(side_effect=AssertionError("completed onboarding needs no lock"))
    add = AsyncMock(side_effect=AssertionError("deleted examples must stay deleted"))
    monkeypatch.setattr(starter_reports.users, "lock_user", lock)
    monkeypatch.setattr(starter_reports.reports, "add_completed_reports", add)

    assert asyncio.run(starter_reports.ensure_starter_reports(object(), user)) == 0
    lock.assert_not_awaited()
    add.assert_not_awaited()


def test_report_ids_are_stable_per_owner_and_distinct_between_owners():
    created_at = datetime(2026, 9, 14, tzinfo=timezone.utc)
    common = {
        "strategy_key": "portfolio_1",
        "strategy_name": "Volatility Momentum",
        "universe": ["AAPL", "MSFT"],
        "created_at": created_at,
    }
    first = starter_reports._build_report(owner_id=OWNER, **common)
    repeated = starter_reports._build_report(owner_id=OWNER, **common)
    other = starter_reports._build_report(owner_id=OTHER_OWNER, **common)

    assert first.id == repeated.id
    assert first.id != other.id
    assert first.equity_curve[-1].date == "2026-09-14"


def test_starter_is_staged_as_an_ordinary_owned_report():
    detail = starter_reports._build_report(
        owner_id=OWNER,
        strategy_key="portfolio_1",
        strategy_name="Volatility Momentum",
        universe=["AAPL", "MSFT"],
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    session = SimpleNamespace(add_all=Mock(), flush=AsyncMock())

    asyncio.run(reports.add_completed_reports(session, OWNER, [detail]))

    saved = session.add_all.call_args.args[0][0]
    assert saved.id == uuid.UUID(detail.id)
    assert saved.owner_id == OWNER
    assert saved.strategy_key == "portfolio_1"
    assert saved.results["reportMetadata"]["starterExample"]["deletable"] is True
    assert "status" not in saved.results
    session.flush.assert_awaited_once()


@pytest.mark.parametrize("unavailable", [None, "disabled", "user", "archived"])
def test_unavailable_builtin_defers_without_marking_user(monkeypatch, unavailable):
    user = _user()
    monkeypatch.setattr(
        starter_reports.users, "lock_user", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        starter_reports.reports,
        "owner_has_visible_reports",
        AsyncMock(return_value=False),
    )
    strategy = _strategy("portfolio_1") if unavailable is not None else None
    if unavailable == "disabled":
        strategy.enabled = False
    elif unavailable == "user":
        strategy.kind = "user"
    elif unavailable == "archived":
        strategy.status = "archived"
    monkeypatch.setattr(
        starter_reports.strategies,
        "get_strategy",
        AsyncMock(return_value=strategy),
    )
    add = AsyncMock()
    monkeypatch.setattr(starter_reports.reports, "add_completed_reports", add)

    assert asyncio.run(starter_reports.ensure_starter_reports(object(), user)) == 0
    assert user.starter_reports_seeded_at is None
    add.assert_not_awaited()


def test_locked_marker_prevents_deleted_examples_being_recreated(monkeypatch):
    stale_user = _user()
    locked_user = _user()
    locked_user.starter_reports_seeded_at = datetime.now(timezone.utc)
    monkeypatch.setattr(
        starter_reports.users, "lock_user", AsyncMock(return_value=locked_user)
    )
    history = AsyncMock(side_effect=AssertionError("locked marker already settles onboarding"))
    monkeypatch.setattr(starter_reports.reports, "owner_has_visible_reports", history)

    assert asyncio.run(starter_reports.ensure_starter_reports(object(), stale_user)) == 0
    history.assert_not_awaited()


def test_failed_report_insert_does_not_set_onboarding_marker(monkeypatch):
    user = _user()
    monkeypatch.setattr(starter_reports.users, "lock_user", AsyncMock(return_value=user))
    monkeypatch.setattr(starter_reports.reports, "owner_has_visible_reports", AsyncMock(return_value=False))
    monkeypatch.setattr(starter_reports.strategies, "get_strategy", AsyncMock(side_effect=lambda session, key: _strategy(key)))
    monkeypatch.setattr(starter_reports.reports, "add_completed_reports", AsyncMock(side_effect=RuntimeError("insert failed")))

    with pytest.raises(RuntimeError, match="insert failed"):
        asyncio.run(starter_reports.ensure_starter_reports(object(), user))
    assert user.starter_reports_seeded_at is None
