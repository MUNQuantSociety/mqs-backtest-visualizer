"""Does the S3 strategy store work against a real bucket, and how fast is it?

Opt-in, because it needs a bucket and credentials that a laptop or CI may not
have. It exercises exactly the code path a deploy uses — ``S3StrategyStore``
from ``src/integrations/strategy_store.py`` with the SDK's default credential
chain — against a throwaway key under ``strategies/_verify/<uuid>/`` and removes it
again, so it can be run on a live bucket without touching anything else that
lives there. Each application operation (list, get, put, delete) is exercised
and timed; the real starter template also goes through the engine loader.

    venv/Scripts/python.exe scripts/verify_s3_store.py --bucket NAME [--prefix P]
        [--region R] [--endpoint-url URL]

It refuses to run without ``--bucket``: there is no default bucket, and it
must never guess one from the environment and write into it. Exit code is 0
when every check passed, 1 otherwise, so it can gate a deploy. No credential
is ever printed; the bucket name and key are the only identifiers shown.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Run as a file, not a module: put the repo root on the path so ``src``
# imports the same way it does under uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.integrations.strategy_store import S3StrategyStore, StrategyStoreError
from src.services.strategy_validation.packaging import build_config
from src.services.strategy_validation.template import STARTER_SOURCE

VERIFY_PREFIX = "strategies/_verify"

SOURCE = STARTER_SOURCE
CONFIG = json.dumps(build_config("verify"), indent=2) + "\n"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    ms: float | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, ms: float | None = None) -> None:
        self.checks.append(Check(name, ok, detail, ms))

    @property
    def failed(self) -> bool:
        return any(not c.ok for c in self.checks)


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - start) * 1000


def _one_line(exc: BaseException) -> str:
    if not isinstance(exc, (StrategyStoreError, KeyError, ValueError)):
        # SDK exceptions can contain authenticated endpoint URLs.
        return type(exc).__name__
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_prefix_probe(report: Report, store: S3StrategyStore) -> bool:
    """Probe ListObjectsV2 inside the strategy namespace, honoring store.prefix.

    An empty prefix is a successful access check. The store translates bucket
    and permission failures into StrategyStoreError; prefix-scoped task roles
    do not need unrestricted bucket-level access for this check.
    """
    key = f"{VERIFY_PREFIX}/probe/"
    try:
        _, ms = _timed(lambda: store.exists(key))
    except StrategyStoreError as exc:
        report.add("prefix_probe", False, _one_line(exc))
        return False
    report.add("prefix_probe", True, f"s3://{store.bucket}/{store.prefix}{key} list allowed", ms)
    return True


def check_round_trip(report: Report, store: S3StrategyStore, key: str, workdir: Path) -> None:
    def step(name: str, fn, verify=None) -> bool:
        try:
            result, ms = _timed(fn)
            if verify is not None:
                ok, detail = verify(result)
                report.add(name, ok, detail, ms)
                return ok
        except Exception as exc:
            report.add(name, False, _one_line(exc))
            return False
        report.add(name, True, "", ms)
        return True

    if not step("put strategy.py", lambda: store.put(key, "strategy.py", SOURCE)):
        return
    if not step("put config.json", lambda: store.put(key, "config.json", CONFIG)):
        return
    step(
        "get strategy.py",
        lambda: store.get(key, "strategy.py"),
        lambda got: (got == SOURCE, "content matches" if got == SOURCE else "content differs"),
    )
    step(
        "get config.json",
        lambda: store.get(key, "config.json"),
        lambda got: (got == CONFIG, "content matches" if got == CONFIG else "content differs"),
    )
    step(
        "exists after put",
        lambda: store.exists(key),
        lambda got: (got is True, f"exists={got}"),
    )

    def _materialize_check(dest: Path) -> tuple[bool, str]:
        have = sorted(p.name for p in Path(dest).iterdir())
        ok = have == ["config.json", "strategy.py"] and (
            Path(dest) / "strategy.py"
        ).read_text(encoding="utf-8") == SOURCE and (
            Path(dest) / "config.json"
        ).read_text(encoding="utf-8") == CONFIG
        return ok, f"files={have}"

    if step("materialize", lambda: store.materialize(key, workdir / "pkg"), _materialize_check):
        def _load_template():
            from engine.strategies.user_loader import load_user_strategy, module_name_for

            token = uuid.uuid4().hex
            try:
                loaded = load_user_strategy(
                    storage_key=key, store=store, dest_dir=workdir / "loaded", token=token
                )
                return loaded.strategy_class.__name__
            finally:
                sys.modules.pop(module_name_for(token), None)

        step("engine loader", _load_template, lambda name: (name == "MyStrategy", name))
    step("delete", lambda: store.delete(key))
    step(
        "exists after delete",
        lambda: store.exists(key),
        lambda got: (got is False, f"exists={got}"),
    )

    def _get_after_delete():
        try:
            store.get(key, "strategy.py")
        except KeyError:
            return "KeyError"
        return "no error"

    step(
        "get after delete",
        _get_after_delete,
        lambda got: (got == "KeyError", f"raised {got}"),
    )


# ---------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live round-trip check of the S3 strategy store against one bucket."
    )
    parser.add_argument("--bucket", required=True, help="bucket to test; required, never guessed")
    parser.add_argument("--prefix", default="", help="key prefix inside the bucket (optional)")
    parser.add_argument("--region", default=None, help="AWS region (default: the SDK's)")
    parser.add_argument("--endpoint-url", default=None, help="LocalStack/MinIO endpoint (optional)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; a gate script must not crash on its
    # own report, so degrade unprintable characters instead of raising.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = _parse(sys.argv[1:] if argv is None else argv)
    bucket = (args.bucket or "").strip()
    if not bucket:
        print("refusing to run: --bucket must name the bucket to test", file=sys.stderr)
        return 2

    try:
        store = S3StrategyStore(
            bucket, prefix=args.prefix, region=args.region, endpoint_url=args.endpoint_url
        )
    except ValueError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2

    # A key nobody else will ever hold, and one the store's own guard accepts.
    key = f"{VERIFY_PREFIX}/{uuid.uuid4().hex}/"
    report = Report()

    print(
        f"target  s3://{store.bucket}/{store.prefix}{key}"
        f"  region={store.region or '(sdk default)'}"
        f"  endpoint={'custom' if store.endpoint_url else '(aws)'}"
    )
    print()

    with tempfile.TemporaryDirectory(prefix="mqs-verify-s3-") as workdir:
        owned = False
        try:
            if check_prefix_probe(report, store):
                if store.exists(key):
                    report.add("fresh key", False, "verification prefix already exists; left untouched")
                else:
                    owned = True
                    check_round_trip(report, store, key, Path(workdir))
        except Exception as exc:
            report.add("round trip", False, _one_line(exc))
        finally:
            # A timeout can arrive after a successful write. Sweep our fresh
            # prefix even when the first put failed, never a preexisting key.
            if owned:
                try:
                    store.delete(key)
                except Exception as exc:
                    report.add("cleanup delete", False, _one_line(exc))

    width = max((len(c.name) for c in report.checks), default=8)
    for c in report.checks:
        mark = "OK " if c.ok else "FAIL"
        lat = f"{c.ms:8.1f} ms" if c.ms is not None else " " * 11
        print(f"{mark}  {c.name:<{width}}  {lat}  {c.detail}")

    print()
    print("RESULT: " + ("all checks passed" if not report.failed else "FAILURES above"))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
