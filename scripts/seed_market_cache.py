"""Warm the parquet market-data cache from an MQSMaster checkout.

Optional convenience, not a dependency: the engine happily builds the cache
itself, but the first run over a fresh ticker set means minutes of slab queries
against a remote university database. If a copy of MQSMaster is on this
machine, its already-backfilled parquet files are exactly what the engine would
have produced, so copying them turns that first run into a cache hit.

    venv/Scripts/python.exe scripts/seed_market_cache.py
    venv/Scripts/python.exe scripts/seed_market_cache.py --source <path> --force

Nothing here reads the database, and files that already exist are left alone
unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.data.cache import cache_dir  # noqa: E402

DEFAULT_SOURCE = Path(
    r"C:\Users\user\OneDrive\Desktop\MQSMaster\src\backtest\data\backfill_cache"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="MQSMaster backfill_cache directory to copy from",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite parquet files that already exist here",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"No cache to seed from: {args.source} does not exist.")
        print("This is not an error — the engine will build the cache on demand.")
        return 0

    destination_dir = cache_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for source_file in sorted(args.source.glob("*.parquet")):
        destination = destination_dir / source_file.name
        if destination.exists() and not args.force:
            skipped += 1
            continue
        shutil.copy2(source_file, destination)
        copied += 1
        print(f"  + {source_file.name} ({source_file.stat().st_size / 1e6:.1f} MB)")

    print(
        f"Seeded {copied} parquet file(s) into {destination_dir} "
        f"({skipped} already present)."
    )
    if copied or skipped:
        print(
            "Note: reading these needs a parquet engine (pyarrow). Without one the "
            "cache silently misses and every run queries the database instead."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
