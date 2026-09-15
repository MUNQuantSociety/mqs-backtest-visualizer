"""Re-running the dev seed over a range that overlaps stored bars.

The table dedupes on (ticker, timestamp), so the danger was never duplicate
rows. It was the walk: regenerating bars already stored produces a different
series from the one in the table, and the first new bar then continued from
that discarded series instead of the stored close.
"""

import random
from datetime import date, time

from scripts.seed_dev_db import BAR_TIMES, bars_for_ticker, missing_runs, requested_slots, trading_days


DAYS = trading_days(date(2026, 3, 13), 10)  # 2026-03-02 .. 2026-03-13


def stored_through(last_day: date, *, first_day: date = date(2000, 1, 1)) -> set:
    return {(d, t) for d in DAYS if first_day <= d <= last_day for t in BAR_TIMES}


def test_an_empty_table_generates_every_requested_bar_as_one_run():
    assert missing_runs(DAYS, set()) == [requested_slots(DAYS)]


def test_stored_days_are_skipped_and_only_the_extension_remains():
    runs = missing_runs(DAYS, stored_through(date(2026, 3, 9)))

    assert runs == [requested_slots([d for d in DAYS if d > date(2026, 3, 9)])]


def test_days_older_than_the_stored_series_are_one_backfill_run():
    stored = {(d, t) for d in DAYS if d >= date(2026, 3, 10) for t in BAR_TIMES}

    assert missing_runs(DAYS, stored) == [requested_slots([d for d in DAYS if d < date(2026, 3, 10)])]


def test_a_range_already_stored_generates_nothing():
    assert missing_runs(DAYS, set(requested_slots(DAYS))) == []


def test_a_gap_inside_the_stored_series_is_its_own_run():
    stored = set(requested_slots(DAYS)) - set(requested_slots([date(2026, 3, 5), date(2026, 3, 6)]))

    assert missing_runs(DAYS, stored) == [requested_slots([date(2026, 3, 5), date(2026, 3, 6)])]


def test_two_gaps_are_two_runs_even_when_only_one_stored_day_separates_them():
    stored = set(requested_slots([date(2026, 3, 4), date(2026, 3, 11)]))

    runs = missing_runs(DAYS, stored)

    assert [run[0][0] for run in runs] == [date(2026, 3, 2), date(2026, 3, 5), date(2026, 3, 12)]
    assert [run[-1][0] for run in runs] == [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 13)]
    assert sum(len(run) for run in runs) == (len(DAYS) - 2) * len(BAR_TIMES)


def test_a_half_written_day_is_completed_from_its_last_stored_bar():
    # An interrupted seed stopped after the 12:30 bar on the 9th.
    stored = stored_through(date(2026, 3, 6)) | {(date(2026, 3, 9), t) for t in BAR_TIMES[:4]}

    (run,) = missing_runs(DAYS, stored)

    assert run[0] == (date(2026, 3, 9), time(13, 30))
    assert run[:4] == [(date(2026, 3, 9), t) for t in BAR_TIMES[4:]]
    assert run[4] == (date(2026, 3, 10), BAR_TIMES[0])


def test_the_extension_opens_at_the_close_it_is_anchored_to():
    (extension,) = missing_runs(DAYS, stored_through(date(2026, 3, 9)))

    rows = bars_for_ticker("AAPL", extension, random.Random(1), 123.45)

    assert rows[0][4] == 123.45  # open_price of the first new bar
    assert rows[0][2] == date(2026, 3, 10)
    assert rows[0][1].time() == BAR_TIMES[0]
