"""Copy registry-referenced local packages into an already provisioned S3 bucket.

Run a dry-run first (the default), then stop ALL API instances and workers,
including validation workers, and keep them stopped until verification and
the coordinated backend switch/restart are complete. Apply requires the
operator's explicit --api-workers-stopped acknowledgement; this script cannot
observe processes on other hosts. Do not edit local packages during cutover.

    venv/Scripts/python.exe scripts/migrate_strategy_store_s3.py --bucket NAME --prefix PREFIX
    venv/Scripts/python.exe scripts/migrate_strategy_store_s3.py --bucket NAME --prefix PREFIX --apply --api-workers-stopped

Use --prefix / explicitly for the bucket root. The source is always
LocalStrategyStore(settings.strategy_store_root), even if the configured
backend is already S3. All user storage_key references are included regardless
of status/enabled state; built-ins and null references are excluded. No schema
initialization, registry updates, bucket provisioning, role changes or deletes
are performed. Unreferenced files are never copied. Registry keys are retained.

The complete inventory is checked before any writes. Defaults bound a run to
1000 packages and each object to 1 MiB; --max-packages may raise the former
explicitly. Registry reads use a 5s connect timeout, a read-only transaction
and 15s statement timeout; S3 uses the store's bounded timeouts/retries. Only
one package's bytes are held at a time. Conditional puts never overwrite an
existing current object, including a concurrent writer's object. Every
completed package is verified byte-for-byte and with SHA-256, not ETags.

S3 does not atomically write two objects: interruption may leave a partial
destination package. Keep services stopped and rerun; equal objects are
accepted and only missing objects are written. Local originals are retained
for rollback. Do not switch backends after any nonzero exit; 0 means a clean
dry-run or fully verified apply, 1 an inventory/copy/verification failure, and
2 invalid CLI usage. Reconcile post-cutover uploads/deletions before rollback.
Output contains counts and keys, never contents, hashes or raw SDK/DB errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings
from src.integrations.strategy_store import (
    LocalStrategyStore,
    S3StrategyStore,
    _content_type_for,
    _key_segments,
)

FILENAMES = ("strategy.py", "config.json")
DEFAULT_MAX_PACKAGES = 1000
MAX_OBJECT_BYTES = 1024 * 1024


class MigrationError(RuntimeError):
    """Only controlled messages and validated keys may be printed by the CLI."""


@dataclass(frozen=True)
class ObjectSnapshot:
    size: int
    digest: bytes


@dataclass
class Report:
    packages: int = 0
    objects: int = 0
    equal: int = 0
    missing: int = 0
    copied: int = 0
    verified: int = 0

    def summary(self) -> str:
        return " ".join(f"{name}={value}" for name, value in vars(self).items())


def read_registry_keys(max_packages: int) -> list[str]:
    """One bounded SELECT; closing the read-only connection rolls it back."""
    from sqlalchemy import select
    from src.models.strategies import Strategy

    statement = (
        select(Strategy.storage_key)
        .where(Strategy.kind == "user", Strategy.storage_key.is_not(None))
        .distinct()
        .order_by(Strategy.storage_key)
        .limit(max_packages + 1)
    )
    # An operational connection uses the same settings URL without startup
    # hooks or a reusable application pool. Bound connection establishment too.
    engine = create_engine(
        settings.database_url_sync,
        connect_args={"connect_timeout": 5},
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            connection = connection.execution_options(postgresql_readonly=True)
            # Transaction-local settings, not persisted configuration changes.
            connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            return list(connection.execute(statement).scalars())
    finally:
        engine.dispose()


def _label(key: str, filename: str = "") -> str:
    return json.dumps(key + filename, ensure_ascii=True)


def _snapshot(content: bytes) -> ObjectSnapshot:
    return ObjectSnapshot(len(content), hashlib.sha256(content).digest())


def _local_bytes(source: LocalStrategyStore, key: str, filename: str) -> bytes:
    try:
        # The store validates traversal, Windows names and linked paths. Read
        # bounded raw bytes so BOMs, CRLF and non-UTF8 content are not changed.
        with source._object_path(key, filename).open("rb") as handle:
            content = handle.read(MAX_OBJECT_BYTES + 1)
    except Exception:
        raise MigrationError(f"local-read-failed key={_label(key, filename)}") from None
    if len(content) > MAX_OBJECT_BYTES:
        raise MigrationError(f"local-object-too-large key={_label(key, filename)}")
    return content


def _remote_bytes(destination: S3StrategyStore, key: str, filename: str) -> bytes | None:
    object_key = destination._object_key(key, filename)
    try:
        response = destination._s3.get_object(Bucket=destination.bucket, Key=object_key)
        with closing(response["Body"]) as body:
            if response["ContentLength"] > MAX_OBJECT_BYTES:
                raise MigrationError(f"destination-object-too-large key={_label(key, filename)}")
            content = body.read(MAX_OBJECT_BYTES + 1)
            if len(content) != response["ContentLength"] or len(content) > MAX_OBJECT_BYTES:
                raise MigrationError(f"destination-read-incomplete key={_label(key, filename)}")
        return content
    except MigrationError:
        raise
    except Exception as exc:
        if isinstance(destination._translate(exc, object_key, missing_object=True), KeyError):
            return None
        raise MigrationError(f"destination-read-failed key={_label(key, filename)}") from None


def _check_equal(expected: bytes, actual: bytes | None, key: str, filename: str) -> None:
    if actual is None or actual != expected or _snapshot(actual) != _snapshot(expected):
        raise MigrationError(f"destination-conflict-or-mismatch key={_label(key, filename)}")


def _put_missing(destination: S3StrategyStore, key: str, filename: str, content: bytes) -> bool:
    """The store's normal put overwrites, so use its client for this conditional put."""
    object_key = destination._object_key(key, filename)
    try:
        destination._s3.put_object(
            Bucket=destination.bucket,
            Key=object_key,
            Body=content,
            ContentType=_content_type_for(object_key),
            IfNoneMatch="*",
        )
        return True
    except Exception as exc:
        # A concurrent equal write is a safe no-op. 409/412 with absent or
        # different bytes fails closed; retrying the whole script is bounded.
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
            _check_equal(content, _remote_bytes(destination, key, filename), key, filename)
            return False
        raise MigrationError(f"destination-write-failed key={_label(key, filename)}") from None


def migrate(
    source: LocalStrategyStore,
    destination: S3StrategyStore,
    *,
    inventory: Callable[[int], Iterable[str]],
    apply: bool = False,
    api_workers_stopped: bool = False,
    max_packages: int = DEFAULT_MAX_PACKAGES,
    report: Report | None = None,
    emit: Callable[[str], None] = print,
) -> Report:
    """Plan first, then optionally copy. Inventory is injectable for offline tests."""
    report = report if report is not None else Report()
    if apply and not api_workers_stopped:
        raise MigrationError("apply-requires-api-workers-stopped")
    if max_packages < 1:
        raise MigrationError("max-packages-must-be-positive")
    try:
        references = list(islice(inventory(max_packages), max_packages + 1))
    except Exception:
        raise MigrationError("registry-inventory-failed") from None
    if len(references) > max_packages:
        raise MigrationError("registry-exceeds-max-packages")

    keys: set[str] = set()
    for key in references:
        try:
            parts = _key_segments(key)
            # Refuse normalization/aliasing instead of silently changing a
            # registry reference. This is the layout generated by packaging.
            if len(parts) < 2 or parts[0] != "strategies" or key != "/".join(parts) + "/":
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise MigrationError("invalid-registry-storage-key") from None
        keys.add(key)

    report.packages = len(keys)
    report.objects = report.packages * len(FILENAMES)
    snapshots: dict[str, dict[str, ObjectSnapshot]] = {}
    for key in sorted(keys):
        snapshots[key] = {
            filename: _snapshot(_local_bytes(source, key, filename)) for filename in FILENAMES
        }
    # All packages must be present locally before any remote access, and all
    # destination conflicts must be found before the first possible write.
    for key, files in snapshots.items():
        missing = 0
        for filename, snapshot in files.items():
            local = _local_bytes(source, key, filename)
            if _snapshot(local) != snapshot:
                raise MigrationError(f"local-changed key={_label(key, filename)}")
            remote = _remote_bytes(destination, key, filename)
            if remote is None:
                report.missing += 1
                missing += 1
            else:
                _check_equal(local, remote, key, filename)
                report.equal += 1
        emit(f"plan key={_label(key)} missing={missing} equal={len(FILENAMES) - missing}")

    if not apply:
        return report

    for key, files in snapshots.items():
        # Load/check both halves before writing either; local files may have
        # changed since the preflight even though services should be stopped.
        contents = {filename: _local_bytes(source, key, filename) for filename in FILENAMES}
        for filename, content in contents.items():
            if _snapshot(content) != files[filename]:
                raise MigrationError(f"local-changed key={_label(key, filename)}")
        missing_files = []
        for filename, content in contents.items():
            remote = _remote_bytes(destination, key, filename)
            if remote is None:
                missing_files.append(filename)
            else:
                _check_equal(content, remote, key, filename)
        for filename in missing_files:
            report.copied += int(_put_missing(destination, key, filename, contents[filename]))
        for filename, content in contents.items():
            _check_equal(content, _remote_bytes(destination, key, filename), key, filename)
        report.verified += 1
        emit(f"verified key={_label(key)} objects={len(FILENAMES)}")
    return report


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main(
    argv: list[str] | None = None,
    *,
    inventory: Callable[[int], Iterable[str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True, help="existing destination bucket; never provisioned")
    parser.add_argument("--prefix", required=True, help="explicit destination prefix; / means bucket root")
    parser.add_argument("--region", default=None, help="region; defaults to configured AWS region")
    parser.add_argument("--endpoint-url", default=None, help="explicit S3-compatible endpoint, if needed")
    parser.add_argument("--apply", action="store_true", help="copy missing objects after complete preflight")
    parser.add_argument("--api-workers-stopped", action="store_true", help="acknowledge all API/worker instances are stopped")
    parser.add_argument("--max-packages", type=_positive_int, default=DEFAULT_MAX_PACKAGES)
    args = parser.parse_args(argv)
    if args.apply and not args.api_workers_stopped:
        parser.error("--apply requires --api-workers-stopped; stop all API and worker instances first")
    if not args.bucket.strip():
        parser.error("--bucket must be nonempty")
    prefix = args.prefix.strip().strip("/")
    try:
        if prefix and "/".join(_key_segments(prefix)) != prefix:
            raise ValueError
    except ValueError:
        parser.error("--prefix must be a canonical relative prefix or / for the bucket root")

    report = Report()
    mode = "apply" if args.apply else "dry-run"
    try:
        source = LocalStrategyStore(settings.strategy_store_root)
        destination = S3StrategyStore(
            args.bucket, prefix=prefix, region=args.region or settings.aws_region or None,
            endpoint_url=args.endpoint_url,
        )
        migrate(
            source, destination, inventory=inventory or read_registry_keys,
            apply=args.apply, api_workers_stopped=args.api_workers_stopped,
            max_packages=args.max_packages, report=report,
        )
    except MigrationError as exc:
        print(f"failed mode={mode} {report.summary()} {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Never print a raw exception: driver/SDK errors may contain credentials,
        # authenticated endpoint URLs, or contents supplied by an SDK mock.
        print(f"failed mode={mode} {report.summary()} unexpected-error", file=sys.stderr)
        return 1
    print(f"complete mode={mode} {report.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
