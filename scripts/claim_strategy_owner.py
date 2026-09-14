"""Give ownerless user strategies an owner, so their uploader can delete them.

``app.strategies.owner_id`` arrived after some uploads did. The boot-time
backfill attributes each old upload from its validation run or saved report;
an upload whose validation never started has neither, so its owner stays NULL
and the API — which lets a member delete or read source only for *their own*
rows — refuses it for everyone. This is the manual step for those rows.

    venv/bin/python scripts/claim_strategy_owner.py --owner <uuid>
    venv/bin/python scripts/claim_strategy_owner.py --owner <uuid> --keys user-a-1,user-b-2 --apply

Dry run by default: it prints what would change as JSON and writes nothing.
``--apply`` performs the update in one transaction. Re-running is idempotent.

Only ``kind = 'user'`` rows with ``owner_id IS NULL`` are ever changed. A
built-in has no owner on purpose and stays that way; a row someone already
owns is never reassigned — that would move a strategy between members, which
is a different operation and not one a bulk script should offer.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


@dataclass(frozen=True)
class StrategyRow:
    key: str
    kind: str
    owner_id: uuid.UUID | None


@dataclass(frozen=True)
class Plan:
    """What one run would do, computed before anything is written."""

    owner: uuid.UUID
    claim: list[str]
    #: key → why it is left alone
    skipped: dict[str, str]
    #: keys that were requested but do not exist
    unknown: list[str]

    @property
    def ok(self) -> bool:
        """True when every requested key is claimable — the only state ``--apply`` accepts."""
        return not self.skipped and not self.unknown

    def as_json(self, *, applied: bool) -> str:
        payload = asdict(self)
        payload["owner"] = str(self.owner)
        payload["applied"] = applied
        return json.dumps(payload, sort_keys=True)


def plan(owner: uuid.UUID, rows: list[StrategyRow], requested: list[str] | None) -> Plan:
    """Decide, from the rows as they are, which keys ``owner`` may claim.

    ``requested`` narrows the run to named keys; ``None`` means every
    ownerless user strategy. Pure so the decision is testable without a
    database: the rules here are the whole policy.
    """
    by_key = {row.key: row for row in rows}
    keys = list(dict.fromkeys(requested)) if requested is not None else sorted(by_key)
    claim: list[str] = []
    skipped: dict[str, str] = {}
    unknown: list[str] = []
    for key in keys:
        row = by_key.get(key)
        if row is None:
            unknown.append(key)
        elif row.kind != "user":
            skipped[key] = f"a {row.kind} strategy has no owner by design"
        elif row.owner_id == owner:
            skipped[key] = "already owned by this owner"
        elif row.owner_id is not None:
            skipped[key] = f"owned by {row.owner_id}; reassignment is not this script's job"
        else:
            claim.append(key)
    return Plan(owner=owner, claim=claim, skipped=skipped, unknown=unknown)


def owner_exists(connection: Connection, owner: uuid.UUID) -> bool:
    """The owner must be an account the API could sign in as.

    Cognito sign-ins map to ``app.users``; the local development identity is a
    ``public.user_creds`` row. Either table may be absent on a given
    database, so each is consulted only if it exists.
    """
    for schema, table in (("app", "users"), ("public", "user_creds")):
        present = connection.execute(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"{schema}.{table}"}
        ).scalar_one()
        if not present:
            continue
        found = connection.execute(
            text(f"SELECT 1 FROM {schema}.{table} WHERE id = :id"), {"id": owner}
        ).scalar_one_or_none()
        if found is not None:
            return True
    return False


def load_rows(connection: Connection, requested: list[str] | None) -> list[StrategyRow]:
    """Every row the plan needs to see: the requested keys, or every ownerless user row."""
    if requested is None:
        statement = text(
            "SELECT key, kind, owner_id FROM app.strategies "
            "WHERE kind = 'user' AND owner_id IS NULL"
        )
        result = connection.execute(statement)
    else:
        statement = text("SELECT key, kind, owner_id FROM app.strategies WHERE key = ANY(:keys)")
        result = connection.execute(statement, {"keys": requested})
    return [StrategyRow(key=key, kind=kind, owner_id=owner_id) for key, kind, owner_id in result]


def claim(engine: Engine, owner: uuid.UUID, requested: list[str] | None, *, apply: bool) -> Plan:
    """Plan, and with ``apply`` perform, the ownership update in one transaction.

    The rows are re-read inside the same transaction that writes them, and
    the UPDATE repeats the plan's guards in SQL, so a row that changed hands
    between the dry run and ``--apply`` is left alone rather than overwritten.
    """
    with engine.begin() as connection:
        if not owner_exists(connection, owner):
            raise ValueError(f"{owner} is not an account in app.users or public.user_creds.")
        decided = plan(owner, load_rows(connection, requested), requested)
        if not apply:
            return decided
        if not decided.ok:
            raise ValueError("Refusing to apply: some requested keys cannot be claimed. Dry-run for details.")
        if decided.claim:
            result = connection.execute(
                text(
                    "UPDATE app.strategies SET owner_id = :owner "
                    "WHERE key = ANY(:keys) AND kind = 'user' AND owner_id IS NULL"
                ),
                {"owner": owner, "keys": decided.claim},
            )
            if result.rowcount != len(decided.claim):
                raise RuntimeError(
                    f"Planned {len(decided.claim)} rows but {result.rowcount} matched; "
                    "the table changed under this run. Nothing was committed."
                )
    return decided


def parse_keys(value: str | None) -> list[str] | None:
    if value is None:
        return None
    keys = [part.strip() for part in value.split(",") if part.strip()]
    if not keys:
        raise argparse.ArgumentTypeError("--keys needs at least one strategy key.")
    return keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--owner", type=uuid.UUID, required=True, help="The account to attribute the rows to.")
    parser.add_argument(
        "--keys", type=parse_keys, default=None,
        help="Comma-separated strategy keys. Default: every ownerless user strategy.",
    )
    parser.add_argument("--apply", action="store_true", help="Write the change. Default is a dry run.")
    args = parser.parse_args(argv)

    from src.db.engine import create_sync_engine

    engine = create_sync_engine()
    try:
        decided = claim(engine, args.owner, args.keys, apply=args.apply)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(decided.as_json(applied=args.apply))
    return 0 if decided.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
