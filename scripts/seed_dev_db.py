"""Fill a *local* ``public.market_data`` with synthetic bars.

The Docker dev database starts with the table but no rows, and every
interesting path through this application — a run, a strategy validation, the
run form's date picker — asks the database what data exists before it does
anything. An empty table therefore does not fail loudly; it produces empty
coverage, a validation window that cannot be built, and a backtest that
"succeeds" over nothing. This script is what turns the container into a
database you can actually develop against.

    venv/bin/python scripts/seed_dev_db.py
    venv/bin/python scripts/seed_dev_db.py --days 365 --tickers AAPL,MSFT

The bars are invented, not real market history: a seeded geometric random walk
per ticker. Nothing in the engine cares whether prices are genuine — it cares
that bars exist, fall inside trading hours, and are self-consistent — and
inventing them keeps this repo free of redistributed market data. Numbers from
a run against this database are therefore meaningful as *plumbing* evidence
and meaningless as *strategy* evidence.

Three details are load-bearing, each learned from a query in this repo:

* **Bars sit inside 09:30–16:00 New York.** ``engine/core/utils.py`` filters on
  ``(timestamp AT TIME ZONE 'America/New_York')::time BETWEEN '09:30' AND
  '16:00'``. Daily closes stamped at midnight pass every other check and then
  return zero rows here.
* **``date`` is the New York date of ``timestamp``, not the UTC date.**
  ``repositories/market_data.py`` reports coverage from the ``date`` column
  while the engine derives its own trading day from ``timestamp AT TIME ZONE``.
  A 16:00 EDT bar is 20:00 UTC the same day, but a UTC-derived date drifts for
  any bar after 20:00 EDT — and then coverage and the engine disagree about
  which days exist.
* **Weekends are skipped, holidays are not.** Modelling the NYSE calendar here
  would be a dependency and a maintenance burden for no gain: the engine reads
  whatever days exist rather than asserting a calendar.

Writes are idempotent (``ON CONFLICT (ticker, timestamp) DO NOTHING``), so
re-running extends coverage rather than duplicating it. Days the table already
holds are skipped rather than regenerated — bar by bar, so a day an
interrupted seed left half-written is completed rather than skipped — and an
extension picks the walk up from the last stored close, so the join is
continuous; a run that *backfills*
days older than everything stored still leaves a seam at the far end, because
there is no earlier close to anchor to.
"""

from __future__ import annotations

import argparse
import random
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Run as a file, not a module: put the repo root on the path so ``src``
# imports the same way it does under uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from psycopg2.extras import execute_values

from src.core.config import settings

NY = ZoneInfo("America/New_York")

# The union of every seeded strategy's universe (engine/strategies/*/config.json),
# so a freshly seeded database can run any of them without a second thought.
DEFAULT_TICKERS = (
    "AAPL", "AMD", "AMZN", "CAT", "GLD", "JPM", "MSFT",
    "NVDA", "TLT", "TSLA", "UNH", "WMT", "XOM", "^VIX",
)

# One bar an hour through the session, plus the close. The engine's seeded
# portfolios use INTERVAL 60 and resample, so hourly is enough resolution to
# exercise every code path while keeping the table small enough to seed in
# seconds.
BAR_TIMES = (
    time(9, 30), time(10, 30), time(11, 30), time(12, 30),
    time(13, 30), time(14, 30), time(15, 30), time(16, 0),
)

# Plausible opening levels, so a chart drawn from this data does not look
# absurd next to a real one. Only the order of magnitude matters.
SEED_PRICES = {
    "AAPL": 190.0, "AMD": 140.0, "AMZN": 175.0, "CAT": 330.0,
    "GLD": 200.0, "JPM": 195.0, "MSFT": 420.0, "NVDA": 120.0,
    "TLT": 92.0, "TSLA": 250.0, "UNH": 500.0, "WMT": 68.0,
    "XOM": 115.0, "^VIX": 15.0,
}

# Hosts this script is willing to write to. `public.market_data` is owned by
# the live trading system; this script is the only thing in the repository
# that INSERTs into it, and pointing it at the warehouse by leaving a
# production .env in place would corrupt the table every other MQS service
# reads. The check is on the host because that is the value that actually
# decides where the rows land.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db", ""}


def trading_days(end: date, count: int) -> list[date]:
    """The ``count`` most recent weekdays ending at ``end``, oldest first."""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:  # Monday..Friday
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


STORED_BARS_SQL = """
    SELECT (timestamp AT TIME ZONE 'America/New_York')::date,
           (timestamp AT TIME ZONE 'America/New_York')::time
    FROM public.market_data
    WHERE ticker = %s
"""

LAST_CLOSE_SQL = """
    SELECT close_price
    FROM public.market_data
    WHERE ticker = %s AND timestamp < %s
    ORDER BY timestamp DESC
    LIMIT 1
"""

Slot = tuple[date, time]


def stored_bars(cursor, ticker: str) -> set[Slot]:
    """Every (New York date, bar time) that already has a row."""
    cursor.execute(STORED_BARS_SQL, (ticker,))
    return {(day, moment) for day, moment in cursor.fetchall()}


def last_close_before(cursor, ticker: str, moment: datetime) -> float | None:
    """The close of the newest stored bar before ``moment``, if there is one."""
    cursor.execute(LAST_CLOSE_SQL, (ticker, moment))
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def requested_slots(days: list[date]) -> list[Slot]:
    """Every bar the run would write, in time order."""
    return [(day, bar_time) for day in days for bar_time in BAR_TIMES]


def missing_runs(days: list[date], stored: set[Slot]) -> list[list[Slot]]:
    """The requested bars not yet stored, grouped into contiguous runs.

    Bars, not days: a day an interrupted seed left half-written is completed
    from its last stored bar rather than skipped or regenerated. Contiguous
    in the requested sequence, not the calendar — two missing bars with a
    stored one between them are two runs, each of which the caller anchors
    to the close just before it. Stored bars are dropped here rather than
    regenerated and left to ``ON CONFLICT``: the walk over them would
    diverge from what the table holds, and the first genuinely new bar would
    then continue from a discarded price instead of the real last close — a
    seam exactly where an extension should join.
    """
    runs: list[list[Slot]] = []
    open_run = False
    for slot in requested_slots(days):
        if slot in stored:
            open_run = False
            continue
        if not open_run:
            runs.append([])
            open_run = True
        runs[-1].append(slot)
    return runs


def bars_for_ticker(
    ticker: str,
    slots: list[Slot],
    rng: random.Random,
    start_price: float | None = None,
) -> list[tuple]:
    """A random walk over ``slots``, one row per (day, bar time).

    ``start_price`` continues an existing series. Without it the walk restarts
    from ``SEED_PRICES``, which is correct for a fresh table and wrong for an
    extension: the first new bar would gap to the seed price from whatever the
    stored series had drifted to, and a backtest crossing that seam reads the
    jump as a real overnight move.
    """
    price = start_price if start_price is not None else SEED_PRICES.get(ticker, 100.0)
    rows: list[tuple] = []
    for day, bar_time in slots:
        # A localized datetime, then stored as timestamptz. Constructing
        # the instant in New York (rather than in UTC and converting) is
        # what makes the DST boundary a non-event: 09:30 is 09:30 to the
        # engine's filter in both halves of the year.
        stamp = datetime.combine(day, bar_time, tzinfo=NY)

        open_price = price
        # ~1% hourly volatility: enough movement for drawdown and Sharpe
        # to be non-degenerate, not so much that prices go negative.
        close_price = max(0.01, open_price * (1.0 + rng.gauss(0.0, 0.01)))
        # High and low must bracket both ends, or candlestick rendering
        # and any high/low logic in a strategy sees an impossible bar.
        high_price = max(open_price, close_price) * (1.0 + abs(rng.gauss(0.0, 0.003)))
        low_price = min(open_price, close_price) * (1.0 - abs(rng.gauss(0.0, 0.003)))
        volume = rng.randint(100_000, 5_000_000)

        rows.append(
            (
                ticker,
                stamp,
                # The New York calendar date of this instant — see the
                # module docstring for why it is derived and not `day`
                # by coincidence.
                stamp.astimezone(NY).date(),
                "NASDAQ",
                round(open_price, 4),
                round(high_price, 4),
                round(low_price, 4),
                round(close_price, 4),
                volume,
                round(rng.uniform(-1.0, 1.0), 4),
            )
        )
        price = close_price
    return rows


INSERT_SQL = """
    INSERT INTO public.market_data (
        ticker, timestamp, date, exchange,
        open_price, high_price, low_price, close_price,
        volume, avg_sentiment
    )
    VALUES %s
    ON CONFLICT (ticker, timestamp) DO NOTHING
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help=(
            "Trading days of history to generate (default: 365). The seeded "
            "portfolios use LOOKBACK_DAYS=90 and validation uses 30, so the "
            "default leaves room for a window that starts well before either."
        ),
    )
    parser.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="Comma-separated symbols (default: every seeded strategy's universe)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="Last trading day, YYYY-MM-DD (default: today in New York)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260910,
        help=(
            "RNG seed. Each ticker's walk is derived from (seed, ticker), so "
            "the same seed and the same --days/--end reproduce a ticker's "
            "prices exactly regardless of what else is in --tickers"
        ),
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Permit writing to a non-local POSTGRES_HOST. This table is owned "
            "by the live trading system — you almost certainly do not want this."
        ),
    )
    args = parser.parse_args()

    host = (settings.postgres_host or "").strip()
    if host.lower() not in LOCAL_HOSTS and not args.allow_remote:
        print(
            f"Refusing to seed: POSTGRES_HOST is {host!r}, which is not a local\n"
            "database. public.market_data belongs to the live trading system and\n"
            "this script INSERTs into it. Point .env at the Docker dev database\n"
            "(see README.Docker.md), or pass --allow-remote if you are certain.",
            file=sys.stderr,
        )
        return 1

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("No tickers given.", file=sys.stderr)
        return 1
    if args.days < 1:
        print("--days must be at least 1.", file=sys.stderr)
        return 1

    end = args.end or datetime.now(NY).date()
    days = trading_days(end, args.days)

    print(
        f"Seeding {len(tickers)} tickers x {len(days)} trading days "
        f"({days[0]} .. {days[-1]}) into {settings.postgres_db} at {host or 'localhost'}"
    )

    connect_kwargs = dict(settings.psycopg2_connect_kwargs)
    # `with psycopg2.connect(...)` scopes a transaction, not the connection, so
    # `closing` is what actually releases the socket.
    with closing(psycopg2.connect(**connect_kwargs)) as connection:
        with connection.cursor() as cursor:
            for ticker in tickers:
                # Derived per ticker rather than drawn from one shared stream:
                # a shared stream makes a ticker's prices depend on which
                # tickers precede it in --tickers, so seeding AAPL,MSFT and
                # then the full universe writes two incompatible AAPL series
                # that ON CONFLICT DO NOTHING then keeps side by side.
                rng = random.Random(f"{args.seed}:{ticker}")
                # Each run of missing bars continues from the close stored
                # just before it; bars the table holds are not regenerated at
                # all. A run with nothing before it (a backfill) has no close
                # to anchor to and restarts from SEED_PRICES, leaving the seam
                # the module docstring describes.
                runs = missing_runs(days, stored_bars(cursor, ticker))
                rows: list[tuple] = []
                for run in runs:
                    first_stamp = datetime.combine(*run[0], tzinfo=NY)
                    start_price = last_close_before(cursor, ticker, first_stamp)
                    rows += bars_for_ticker(ticker, run, rng, start_price)
                # Chunked so one ticker-year is a handful of round trips
                # rather than one statement with tens of thousands of tuples.
                execute_values(cursor, INSERT_SQL, rows, page_size=1000)
                skipped = len(days) * len(BAR_TIMES) - len(rows)
                print(f"  {ticker:<6} {len(rows):>6} bars" + (f" ({skipped} stored bars skipped)" if skipped else ""))
        connection.commit()

    print(f"Done. {len(tickers) * len(days) * len(BAR_TIMES)} bars offered to the table.")
    print("Verify with: venv/bin/python scripts/check_market_data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
