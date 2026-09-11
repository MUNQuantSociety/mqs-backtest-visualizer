"""Publish portfolio_1/portfolio_2 into the configured S3 store and registry.

    venv/Scripts/python.exe scripts/publish_builtin_strategies.py
    venv/Scripts/python.exe scripts/publish_builtin_strategies.py --apply
    venv/Scripts/python.exe scripts/publish_builtin_strategies.py --strategies portfolio_1 --revision --apply

Default is a read-only dry-run. Apply copies source/config verbatim, verifies
both objects, then updates the existing registry IDs without touching runs.
Existing different S3 content is NEVER overwritten. An interrupted equal or
partial publication is safe to retry. Local originals are retained.
Use --revision to publish a corrected builtin under a source/config hash and
atomically change only its registry pointer, preserving the original package.
Deploy the storage-aware worker before applying; do not run these portfolios
during publication. Production bucket/role creation is not this script's job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from scripts.seed_strategies import ENGINE_STRATEGY_ROOT, build_rows
from src.db.engine import create_sync_engine
from src.integrations.strategy_store import S3StrategyStore, get_strategy_store, strategy_key
from src.models import BacktestRun, Strategy

SUPPORTED = ("portfolio_1", "portfolio_2")
FILENAMES = ("strategy.py", "config.json")


def packages_for(keys: list[str], *, revision: bool = False) -> list[dict]:
    """Read only the explicitly supported, trusted vendored packages."""
    if not keys or any(key not in SUPPORTED for key in keys):
        raise ValueError("Only portfolio_1 and portfolio_2 can be published here.")
    rows = {row["key"]: row for row in build_rows()}
    packages = []
    for key in dict.fromkeys(keys):
        files = {
            filename: (ENGINE_STRATEGY_ROOT / key / filename).read_bytes().decode("utf-8")
            for filename in FILENAMES
        }
        compile(files["strategy.py"], f"{key}/strategy.py", "exec")
        config = json.loads(files["config.json"])
        if config["TICKERS"] != rows[key]["universe"]:
            raise ValueError(f"Registry/config universe mismatch for {key}")
        destination = strategy_key(key)
        if revision:
            digest = hashlib.sha256("\0".join(files[name] for name in FILENAMES).encode("utf-8")).hexdigest()
            destination += f"revisions/{digest}/"
        packages.append({"row": rows[key], "storage_key": destination, "files": files})
    return packages


def _remote(store: S3StrategyStore, key: str, filename: str) -> str | None:
    try:
        return store.get(key, filename)
    except KeyError:
        return None


def publish_packages(packages: list[dict], store: S3StrategyStore, *, apply: bool) -> None:
    """Validate every destination before writes, then conditionally fill gaps."""
    pending = []
    for package in packages:
        key = package["storage_key"]
        for filename, content in package["files"].items():
            actual = _remote(store, key, filename)
            if actual is not None and actual != content:
                raise ValueError(f"Refusing to overwrite different content at {key}{filename}")
            if actual is None:
                pending.append((key, filename, content))
    if not apply:
        return

    for key, filename, content in pending:
        try:
            # Same conditional-write contract as the local-to-S3 migration:
            # never overwrite an object created after the inventory read.
            store._s3.put_object(
                Bucket=store.bucket,
                Key=store._object_key(key, filename),
                Body=content.encode("utf-8"),
                ContentType="application/json" if filename.endswith(".json") else "text/x-python; charset=utf-8",
                IfNoneMatch="*",
            )
        except Exception:
            # Concurrent equal publication is a harmless retry; different or
            # absent content must not be registered as successful.
            if _remote(store, key, filename) != content:
                raise RuntimeError(f"Could not publish {key}{filename}") from None
    for package in packages:
        for filename, content in package["files"].items():
            if _remote(store, package["storage_key"], filename) != content:
                raise RuntimeError(f"Verification failed for {package['storage_key']}{filename}")


def publish(keys: list[str], *, apply: bool, revision: bool = False) -> list[str]:
    packages = packages_for(keys, revision=revision)
    store = get_strategy_store()
    if not isinstance(store, S3StrategyStore):
        raise ValueError("Set STRATEGY_STORE_BACKEND=s3 and configure its existing bucket first.")
    engine = create_sync_engine()
    try:
        # Keep metadata changes atomic, preserve run foreign keys and reject
        # collisions with user-owned IDs. The write lock is only on these rows.
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
            statement = select(Strategy.key, Strategy.kind, Strategy.storage_key).where(Strategy.key.in_(keys))
            if apply:
                statement = statement.with_for_update()
            rows = connection.execute(statement).all()
            if revision and {row.key for row in rows} != set(keys):
                raise ValueError("Revision publication requires existing builtin registry entries.")
            for row in rows:
                expected_key = row.storage_key in (None, strategy_key(row.key))
                prior_revision = revision and (row.storage_key or "").startswith(strategy_key(row.key) + "revisions/")
                if row.kind != "builtin" or not (expected_key or prior_revision):
                    raise ValueError(f"Registry entry {row.key} is not the expected builtin.")
            active = connection.execute(
                select(BacktestRun.id).where(
                    BacktestRun.strategy_key.in_(keys),
                    BacktestRun.status.in_(("queued", "running")),
                ).limit(1)
            ).first()
            if active is not None:
                raise ValueError("Wait for these portfolios' queued/running jobs before publication.")
            publish_packages(packages, store, apply=apply)
            if apply:
                for package in packages:
                    if revision:
                        # Keep the old objects and all registry metadata. The
                        # verified package pointer is the only atomic change.
                        connection.execute(update(Strategy).where(
                            Strategy.key == package["row"]["key"], Strategy.kind == "builtin",
                        ).values(storage_key=package["storage_key"]))
                        continue
                    values = {**package["row"], "storage_key": package["storage_key"]}
                    upsert = insert(Strategy).values(**values)
                    written = connection.execute(
                        upsert.on_conflict_do_update(
                            index_elements=[Strategy.key],
                            set_={field: upsert.excluded[field] for field in values if field != "key"},
                            where=Strategy.kind == "builtin",
                        ).returning(Strategy.key)
                    ).scalar_one_or_none()
                    if written is None:
                        raise ValueError("Registry changed during publication; nothing was registered.")
    finally:
        engine.dispose()
    return [package["storage_key"] for package in packages]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revision", action="store_true", help="Publish immutable source/config and update only the existing builtin's package pointer.")
    parser.add_argument("--strategies", nargs="+", choices=SUPPORTED, default=list(SUPPORTED))
    args = parser.parse_args()
    try:
        keys = publish(args.strategies, apply=args.apply, revision=args.revision)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Publication failed ({type(exc).__name__}); no credentials were printed.", file=sys.stderr)
        return 1
    print(("Published and byte-verified: " if args.apply else "Dry-run passed; no changes: ") + ", ".join(keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
