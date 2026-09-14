"""Re-running the dev seed over a range that overlaps stored bars.

The table dedupes on (ticker, timestamp), so the danger was never duplicate
rows. It was the walk: regenerating days already stored produces a different
series from the one in the table, and the first new day then continued from
that discarded series instead of the stored close.
"""

import random
from datetime import date

from scripts.seed_dev_db import bars_for_ticker, missing_runs, trading_days


DAYS = trading_days(date(2026, 3, 13), 10)  # 2026-03-02 .. 2026-03-13


def test_an_empty_table_generates_every_requested_day_as_one_run():
    assert missing_runs(DAYS, set()) == [DAYS]


def test_stored_days_are_skipped_and_only_the_extension_remains():
    stored = {d for d in DAYS if d <= date(2026, 3, 9)}

    assert missing_runs(DAYS, stored) == [[d for d in DAYS if d > date(2026, 3, 9)]]


def test_days_older_than_the_stored_series_are_one_backfill_run():
    stored = {d for d in DAYS if d >= date(2026, 3, 10)}

    assert missing_runs(DAYS, stored) == [[d for d in DAYS if d < date(2026, 3, 10)]]


def test_a_range_already_stored_generates_nothing():
    assert missing_runs(DAYS, set(DAYS)) == []


def test_a_gap_inside_the_stored_series_is_its_own_run():
    stored = set(DAYS) - {date(2026, 3, 5), date(2026, 3, 6)}

    assert missing_runs(DAYS, stored) == [[date(2026, 3, 5), date(2026, 3, 6)]]


def test_two_gaps_are_two_runs_even_when_only_one_stored_day_separates_them():
    stored = {date(2026, 3, 4), date(2026, 3, 11)}

    runs = missing_runs(DAYS, stored)

    assert [run[0] for run in runs] == [date(2026, 3, 2), date(2026, 3, 5), date(2026, 3, 12)]
    assert [run[-1] for run in runs] == [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 13)]
    assert sum(len(run) for run in runs) == len(DAYS) - 2


def test_the_extension_opens_at_the_close_it_is_anchored_to():
    stored = {d for d in DAYS if d <= date(2026, 3, 9)}
    (extension,) = missing_runs(DAYS, stored)

    rows = bars_for_ticker("AAPL", extension, random.Random(1), 123.45)

    assert rows[0][4] == 123.45  # open_price of the first new bar
    assert rows[0][2] == date(2026, 3, 10)
