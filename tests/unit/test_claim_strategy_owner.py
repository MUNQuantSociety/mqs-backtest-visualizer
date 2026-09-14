"""The manual step for uploads the owner backfill could not attribute.

The policy is the whole script: only ownerless user rows change, built-ins
never do, and nothing moves between members. ``plan`` holds that policy and
is exercised here without a database; the SQL repeats the same guards.
"""

import uuid

import pytest

from scripts.claim_strategy_owner import Plan, StrategyRow, main, parse_keys, plan


OWNER = uuid.UUID(int=1)
SOMEONE_ELSE = uuid.UUID(int=2)

ROWS = [
    StrategyRow("user-a", "user", None),
    StrategyRow("user-b", "user", None),
    StrategyRow("user-mine", "user", OWNER),
    StrategyRow("user-theirs", "user", SOMEONE_ELSE),
    StrategyRow("portfolio_1", "builtin", None),
]


def test_without_keys_every_ownerless_user_row_is_claimed_and_nothing_else():
    decided = plan(OWNER, [row for row in ROWS if row.kind == "user" and row.owner_id is None], None)

    assert decided == Plan(owner=OWNER, claim=["user-a", "user-b"], skipped={}, unknown=[])
    assert decided.ok


def test_a_builtin_is_never_given_an_owner():
    decided = plan(OWNER, ROWS, ["portfolio_1"])

    assert decided.claim == []
    assert decided.skipped == {"portfolio_1": "a builtin strategy has no owner by design"}
    assert not decided.ok


def test_a_row_another_member_owns_is_not_reassigned():
    decided = plan(OWNER, ROWS, ["user-theirs"])

    assert decided.claim == []
    assert "reassignment" in decided.skipped["user-theirs"]
    assert not decided.ok


def test_re_running_on_a_row_already_claimed_is_a_no_op():
    decided = plan(OWNER, ROWS, ["user-mine"])

    assert decided.claim == []
    assert decided.skipped == {"user-mine": "already owned by this owner"}


def test_an_unknown_key_is_reported_not_invented():
    decided = plan(OWNER, ROWS, ["user-a", "nope"])

    assert decided.claim == ["user-a"]
    assert decided.unknown == ["nope"]
    assert not decided.ok


def test_requested_keys_are_deduplicated_and_kept_in_order():
    decided = plan(OWNER, ROWS, ["user-b", "user-a", "user-b"])

    assert decided.claim == ["user-b", "user-a"]


def test_the_json_report_names_the_owner_and_whether_it_applied():
    import json

    report = json.loads(plan(OWNER, ROWS, ["user-a"]).as_json(applied=False))

    assert report == {
        "owner": str(OWNER), "claim": ["user-a"], "skipped": {}, "unknown": [], "applied": False,
    }


@pytest.mark.parametrize("value", ["", " ", ","])
def test_an_empty_keys_argument_is_rejected(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        parse_keys(value)


def test_the_cli_requires_an_owner_before_touching_any_engine(monkeypatch, capsys):
    import scripts.claim_strategy_owner as module

    monkeypatch.setattr(
        "src.db.engine.create_sync_engine",
        lambda: pytest.fail("no engine may be built without a valid --owner"),
    )
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2
    assert "--owner" in capsys.readouterr().err
    assert module.plan  # the module imported without a database
