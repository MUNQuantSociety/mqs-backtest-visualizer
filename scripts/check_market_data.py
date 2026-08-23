"""Measure ``public.market_data`` coverage for the seeded strategy universes.

    venv/Scripts/python.exe scripts/check_market_data.py
    venv/Scripts/python.exe scripts/check_market_data.py --all-tickers

Answers the question a backtest window depends on: what date range does each
ticker a seeded strategy trades actually cover? A run over a window the data
does not reach is the failure mode this exists to make visible before anyone
waits ten minutes for an empty result.

**Why the queries look defensive.** ``market_data`` holds order-of-a-billion
rows on a remote host, so anything that touches the heap is minutes, not
seconds:

* ``count(distinct ticker)`` is a full sequential scan and never finishes in a
  useful time. ``--all-tickers`` instead walks the ``(ticker, timestamp)``
  unique index one distinct value at a time (``min(ticker) WHERE ticker > ?``,
  a "skip scan"), which is one index lookup per ticker — but the warehouse
  holds the whole ingested tape (equities, mutual funds, crypto pairs: over
  5,000 distinct symbols before the walk even reaches "G"), so that mode is
  opt-in and bounded by ``--limit``.
* per-ticker ``min``/``max`` use ``timestamp`` rather than ``date``. Both
  describe the same bar, but only ``timestamp`` is in that index, so the
  aggregate is an index lookup instead of a scan.

Every statement runs under a server-side ``statement_timeout``, so a bad plan
fails loudly in seconds rather than hanging the terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg2  # noqa: E402

from scripts.seed_strategies import build_rows  # noqa: E402
from src.core.config import settings  # noqa: E402

_FIRST_TICKER = "SELECT min(ticker) FROM public.market_data"
_NEXT_TICKER = "SELECT min(ticker) FROM public.market_data WHERE ticker > %s"
_TICKER_RANGE = """
SELECT min(timestamp), max(timestamp)
FROM public.market_data
WHERE ticker = %s
"""


def universe_tickers() -> list[str]:
    """Every distinct ticker across the four seeded strategy universes."""
    seen: dict[str, None] = {}
    for row in build_rows():
        for ticker in row["universe"]:
            seen.setdefault(ticker, None)
    return sorted(seen)


def _fmt(moment: datetime | None) -> str:
    return moment.date().isoformat() if moment is not None else "—"


def _connect(timeout_seconds: int):
    connection = psycopg2.connect(**settings.psycopg2_connect_kwargs)
    # Read-only is belt and braces: this script must never be able to write to
    # the trading system's tables, whatever a future edit does.
    connection.set_session(readonly=True, autocommit=True)
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {int(timeout_seconds) * 1000}")
    return connection


def ticker_ranges(connection, tickers: list[str]) -> dict[str, tuple]:
    """First and last bar timestamp per ticker. Absent keys have no data."""
    ranges: dict[str, tuple] = {}
    with connection.cursor() as cursor:
        for ticker in tickers:
            cursor.execute(_TICKER_RANGE, (ticker,))
            first_bar, last_bar = cursor.fetchone()
            if first_bar is not None:
                ranges[ticker] = (first_bar, last_bar)
    return ranges


def all_tickers(connection, limit: int) -> tuple[list[str], bool]:
    """Distinct tickers by index skip scan. Returns (tickers, hit_the_limit)."""
    found: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(_FIRST_TICKER)
        current = cursor.fetchone()[0]
        while current is not None:
            found.append(current)
            if len(found) >= limit:
                return found, True
            cursor.execute(_NEXT_TICKER, (current,))
            current = cursor.fetchone()[0]
    return found, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="server-side statement timeout in seconds (default 60)",
    )
    parser.add_argument(
        "--all-tickers",
        action="store_true",
        help=(
            "also enumerate every distinct ticker in the table — slow and "
            "throughput-dependent (measured between 7 and 170 tickers/second "
            "against the remote host), so expect tens of minutes and use --limit"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200_000,
        help="stop the distinct-ticker walk after this many (default 200000)",
    )
    args = parser.parse_args()

    if not settings.database_configured:
        print("POSTGRES_* settings are missing — copy .env.example to .env.")
        return 2

    connection = _connect(args.timeout)
    try:
        wanted = universe_tickers()
        started = time.monotonic()
        ranges = ticker_ranges(connection, wanted)
        elapsed = time.monotonic() - started

        print(
            f"Seeded strategy universes: {len(wanted)} distinct tickers "
            f"(measured in {elapsed:.1f}s)"
        )
        print(f"  {'ticker':<8} {'first bar':<12} {'last bar':<12}")
        for ticker in wanted:
            first_bar, last_bar = ranges.get(ticker, (None, None))
            print(f"  {ticker:<8} {_fmt(first_bar):<12} {_fmt(last_bar):<12}")

        missing = [ticker for ticker in wanted if ticker not in ranges]

        if args.all_tickers:
            started = time.monotonic()
            found, truncated = all_tickers(connection, args.limit)
            elapsed = time.monotonic() - started
            qualifier = "at least " if truncated else ""
            print()
            print(
                f"public.market_data distinct tickers: {qualifier}{len(found)} "
                f"({elapsed:.0f}s)"
            )
            print(f"  first: {found[0]}   last: {found[-1]}")
    finally:
        connection.close()

    if missing:
        # A seeded universe naming a ticker the warehouse has never ingested is
        # a run that will fail at load time, so it is an error, not a note.
        print()
        print(f"NO DATA for {len(missing)} seeded ticker(s): {', '.join(missing)}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
