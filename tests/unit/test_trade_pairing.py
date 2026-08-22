"""Unit tests for FIFO fill pairing.

Pure and DB-free by construction: ``pair_fills`` is a function of its input.
The arithmetic is asserted by hand-computed numbers rather than by re-deriving
it in the test, because a test that repeats the implementation's formula
proves nothing about whether the formula is right.

    pytest tests/unit/test_trade_pairing.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.services.trade_pairing import TradeRow, pair_fills


def fill(
    day: str,
    ticker: str,
    side: str,
    shares: float,
    price: float,
    fees: float | None = None,
) -> dict:
    """One executor-shaped fill. Mirrors ``BacktestExecutor.trade_log`` entries,
    including the fields this module deliberately ignores."""
    record = {
        "timestamp": datetime.fromisoformat(f"{day}T15:30:00"),
        "portfolio_id": "portfolio_dummy",
        "ticker": ticker,
        "signal_type": side,
        "confidence": 1.0,
        "shares": shares,
        "fill_price": price,
        "cash_after": 0.0,
        "position_size": 0.0,
    }
    if fees is not None:
        record["fees"] = fees
    return record


def test_empty_input_gives_no_trades() -> None:
    assert pair_fills([]) == []


def test_simple_long_round_trip() -> None:
    trades = pair_fills(
        [
            fill("2025-01-02", "AAPL", "BUY", 100, 10.0),
            fill("2025-01-09", "AAPL", "SELL", 100, 12.0),
        ]
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade == TradeRow(
        seq=0,
        symbol="AAPL",
        side="long",
        entry_date="2025-01-02",
        exit_date="2025-01-09",
        entry_price=10.0,
        exit_price=12.0,
        quantity=100,
        pnl=200.0,
        return_pct=0.2,
        fees=0.0,
    )


def test_short_round_trip_profits_when_price_falls() -> None:
    trades = pair_fills(
        [
            fill("2025-02-03", "TSLA", "SELL", 50, 200.0),
            fill("2025-02-10", "TSLA", "BUY", 50, 180.0),
        ]
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "short"
    assert trade.entry_date == "2025-02-03"
    assert trade.entry_price == 200.0
    assert trade.exit_price == 180.0
    # Sold at 200, bought back at 180: a $1,000 gain, positive.
    assert trade.pnl == pytest.approx(1_000.0)
    assert trade.return_pct == pytest.approx(0.1)


def test_short_round_trip_loses_when_price_rises() -> None:
    trades = pair_fills(
        [
            fill("2025-02-03", "TSLA", "SELL", 10, 100.0),
            fill("2025-02-04", "TSLA", "BUY", 10, 110.0),
        ]
    )

    assert trades[0].pnl == pytest.approx(-100.0)
    assert trades[0].return_pct == pytest.approx(-0.1)


def test_partial_close_splits_the_lot_into_two_round_trips() -> None:
    trades = pair_fills(
        [
            fill("2025-03-03", "MSFT", "BUY", 100, 50.0),
            fill("2025-03-05", "MSFT", "SELL", 40, 55.0),
            fill("2025-03-07", "MSFT", "SELL", 60, 45.0),
        ]
    )

    assert len(trades) == 2

    first, second = trades
    assert (first.quantity, first.exit_date) == (40, "2025-03-05")
    assert first.pnl == pytest.approx(200.0)
    assert first.return_pct == pytest.approx(0.1)

    assert (second.quantity, second.exit_date) == (60, "2025-03-07")
    assert second.pnl == pytest.approx(-300.0)
    assert second.return_pct == pytest.approx(-0.1)

    # Both halves keep the original entry.
    assert first.entry_date == second.entry_date == "2025-03-03"
    assert first.entry_price == second.entry_price == 50.0


def test_one_sell_closes_two_lots_oldest_first() -> None:
    trades = pair_fills(
        [
            fill("2025-04-01", "NVDA", "BUY", 30, 10.0),
            fill("2025-04-02", "NVDA", "BUY", 20, 20.0),
            fill("2025-04-03", "NVDA", "SELL", 50, 15.0),
        ]
    )

    assert len(trades) == 2
    # FIFO: the 10.0 lot closes before the 20.0 lot.
    assert [t.entry_price for t in trades] == [10.0, 20.0]
    assert [t.quantity for t in trades] == [30, 20]
    assert trades[0].pnl == pytest.approx(150.0)
    assert trades[1].pnl == pytest.approx(-100.0)
    assert [t.seq for t in trades] == [0, 1]


def test_sell_larger_than_the_long_flips_to_short_in_one_fill() -> None:
    trades = pair_fills(
        [
            fill("2025-05-01", "AMD", "BUY", 100, 10.0),
            fill("2025-05-02", "AMD", "SELL", 150, 12.0),
            fill("2025-05-03", "AMD", "BUY", 50, 11.0),
        ]
    )

    assert len(trades) == 2

    closed_long, closed_short = trades
    assert closed_long.side == "long"
    assert closed_long.quantity == 100
    assert closed_long.pnl == pytest.approx(200.0)

    # The 50 excess shares opened a short at 12.0, covered next day at 11.0.
    assert closed_short.side == "short"
    assert closed_short.quantity == 50
    assert closed_short.entry_date == "2025-05-02"
    assert closed_short.entry_price == 12.0
    assert closed_short.exit_price == 11.0
    assert closed_short.pnl == pytest.approx(50.0)


def test_buy_larger_than_the_short_flips_to_long_in_one_fill() -> None:
    trades = pair_fills(
        [
            fill("2025-05-01", "AMD", "SELL", 40, 20.0),
            fill("2025-05-02", "AMD", "BUY", 100, 18.0),
        ]
    )

    assert len(trades) == 2

    closed_short, open_long = trades
    assert closed_short.side == "short"
    assert closed_short.quantity == 40
    assert closed_short.pnl == pytest.approx(80.0)

    assert open_long.side == "long"
    assert open_long.quantity == 60
    assert open_long.entry_price == 18.0
    assert open_long.exit_date is None


def test_unclosed_remainder_is_reported_flat() -> None:
    trades = pair_fills(
        [
            fill("2025-06-02", "AAPL", "BUY", 100, 10.0),
            fill("2025-06-09", "AAPL", "SELL", 30, 12.0),
        ]
    )

    assert len(trades) == 2

    closed, still_open = trades
    assert closed.quantity == 30
    assert closed.exit_date == "2025-06-09"

    # Open lots realise nothing: no exit, no P&L (documented choice).
    assert still_open.quantity == 70
    assert still_open.exit_date is None
    assert still_open.exit_price is None
    assert still_open.pnl == 0.0
    assert still_open.return_pct == 0.0
    assert still_open.entry_date == "2025-06-02"
    assert still_open.entry_price == 10.0


def test_fees_are_pro_rated_across_the_lots_a_fill_touches() -> None:
    trades = pair_fills(
        [
            # $20 across 100 shares = $0.20/share.
            fill("2025-07-01", "AAPL", "BUY", 100, 10.0, fees=20.0),
            # $6 across 40 shares = $0.15/share.
            fill("2025-07-02", "AAPL", "SELL", 40, 11.0, fees=6.0),
        ]
    )

    closed, still_open = trades

    # 40 shares * (0.20 entry + 0.15 exit).
    assert closed.fees == pytest.approx(14.0)
    # The untouched 60 shares carry only their entry fees.
    assert still_open.fees == pytest.approx(12.0)
    # Every dollar of the $26 charged is accounted for exactly once.
    assert closed.fees + still_open.fees == pytest.approx(26.0)

    # Fees do not leak into P&L — it stays (exit - entry) * qty.
    assert closed.pnl == pytest.approx(40.0)


def test_fees_split_across_two_lots_closed_by_one_fill() -> None:
    trades = pair_fills(
        [
            fill("2025-07-01", "AAPL", "BUY", 30, 10.0, fees=3.0),
            fill("2025-07-02", "AAPL", "BUY", 70, 10.0, fees=7.0),
            fill("2025-07-03", "AAPL", "SELL", 100, 11.0, fees=50.0),
        ]
    )

    assert [t.fees for t in trades] == [
        pytest.approx(3.0 + 15.0),
        pytest.approx(7.0 + 35.0),
    ]


def test_tickers_are_paired_independently() -> None:
    trades = pair_fills(
        [
            fill("2025-08-01", "AAPL", "BUY", 10, 100.0),
            fill("2025-08-02", "MSFT", "BUY", 10, 200.0),
            fill("2025-08-03", "MSFT", "SELL", 10, 210.0),
            fill("2025-08-04", "AAPL", "SELL", 10, 90.0),
        ]
    )

    assert [(t.symbol, t.pnl) for t in trades] == [
        ("MSFT", pytest.approx(100.0)),
        ("AAPL", pytest.approx(-100.0)),
    ]


def test_reopening_after_a_full_close_starts_a_fresh_lot() -> None:
    trades = pair_fills(
        [
            fill("2025-09-01", "AAPL", "BUY", 10, 10.0),
            fill("2025-09-02", "AAPL", "SELL", 10, 11.0),
            fill("2025-09-03", "AAPL", "BUY", 10, 12.0),
            fill("2025-09-04", "AAPL", "SELL", 10, 13.0),
        ]
    )

    assert len(trades) == 2
    assert [t.entry_date for t in trades] == ["2025-09-01", "2025-09-03"]
    assert [t.entry_price for t in trades] == [10.0, 12.0]


def test_seq_is_contiguous_and_starts_at_zero() -> None:
    trades = pair_fills(
        [
            fill("2025-10-01", "AAPL", "BUY", 90, 10.0),
            fill("2025-10-02", "AAPL", "SELL", 30, 11.0),
            fill("2025-10-03", "AAPL", "SELL", 30, 12.0),
            fill("2025-10-04", "MSFT", "BUY", 10, 50.0),
        ]
    )

    assert [t.seq for t in trades] == list(range(len(trades)))
    # Closed round trips first, then the remainders in the order they opened.
    assert [t.exit_date is None for t in trades] == [False, False, True, True]


def test_pairing_is_deterministic_across_calls() -> None:
    fills = [
        fill("2025-11-03", "AAPL", "BUY", 100, 10.0),
        fill("2025-11-04", "MSFT", "SELL", 20, 30.0),
        fill("2025-11-05", "AAPL", "SELL", 60, 11.0),
        fill("2025-11-06", "MSFT", "BUY", 20, 28.0),
    ]

    assert pair_fills(fills) == pair_fills(fills)


def test_fills_are_sorted_chronologically_before_pairing() -> None:
    ordered = [
        fill("2025-12-01", "AAPL", "BUY", 10, 10.0),
        fill("2025-12-02", "AAPL", "SELL", 10, 12.0),
    ]

    assert pair_fills(list(reversed(ordered))) == pair_fills(ordered)


def test_mixed_naive_and_aware_timestamps_still_sort() -> None:
    aware = fill("2025-12-01", "AAPL", "BUY", 10, 10.0)
    aware["timestamp"] = datetime(2025, 12, 1, 20, 30, tzinfo=timezone.utc)

    trades = pair_fills([fill("2025-12-02", "AAPL", "SELL", 10, 12.0), aware])

    assert len(trades) == 1
    assert trades[0].side == "long"
    assert trades[0].pnl == pytest.approx(20.0)


def test_date_and_iso_string_timestamps_are_accepted() -> None:
    trades = pair_fills(
        [
            {
                "timestamp": date(2026, 1, 5),
                "ticker": "AAPL",
                "signal_type": "buy",
                "shares": 5,
                "fill_price": 10.0,
            },
            {
                "timestamp": "2026-01-06T15:30:00",
                "ticker": "AAPL",
                "signal_type": "sell",
                "shares": 5,
                "fill_price": 11.0,
            },
        ]
    )

    assert len(trades) == 1
    assert (trades[0].entry_date, trades[0].exit_date) == ("2026-01-05", "2026-01-06")


def test_fractional_shares_close_the_lot_without_float_dust() -> None:
    trades = pair_fills(
        [
            fill("2026-02-02", "AAPL", "BUY", 100, 10.0),
            fill("2026-02-03", "AAPL", "SELL", 33, 10.0),
            fill("2026-02-04", "AAPL", "SELL", 33, 10.0),
            fill("2026-02-05", "AAPL", "SELL", 34, 10.0),
        ]
    )

    # Three closed slices and no phantom open remainder.
    assert len(trades) == 3
    assert all(trade.exit_date is not None for trade in trades)


def test_zero_share_fills_are_ignored() -> None:
    trades = pair_fills(
        [
            fill("2026-03-02", "AAPL", "BUY", 0, 10.0),
            fill("2026-03-03", "AAPL", "BUY", 10, 10.0),
            fill("2026-03-04", "AAPL", "SELL", 10, 11.0),
        ]
    )

    assert len(trades) == 1
    assert trades[0].entry_date == "2026-03-03"


def test_zero_entry_price_does_not_divide_by_zero() -> None:
    trades = pair_fills(
        [
            fill("2026-04-01", "AAPL", "BUY", 10, 0.0),
            fill("2026-04-02", "AAPL", "SELL", 10, 5.0),
        ]
    )

    assert trades[0].pnl == pytest.approx(50.0)
    assert trades[0].return_pct == 0.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ticker", None),
        ("signal_type", "HOLD"),
        ("fill_price", None),
        ("timestamp", None),
        ("timestamp", "not-a-date"),
    ],
)
def test_malformed_fills_are_rejected_loudly(key: str, value: object) -> None:
    broken = fill("2026-05-01", "AAPL", "BUY", 10, 10.0)
    broken[key] = value

    with pytest.raises(ValueError) as excinfo:
        pair_fills([broken])

    # The index makes a bad fill findable in a log of thousands.
    assert "fill 0" in str(excinfo.value)


def test_as_row_carries_every_run_trades_column() -> None:
    trades = pair_fills(
        [
            fill("2026-06-01", "AAPL", "BUY", 10, 10.0, fees=1.0),
            fill("2026-06-02", "AAPL", "SELL", 10, 11.0, fees=1.0),
        ]
    )

    row = trades[0].as_row("11111111-2222-3333-4444-555555555555")
    assert set(row) == {
        "run_id",
        "seq",
        "symbol",
        "side",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "quantity",
        "pnl",
        "return_pct",
        "fees",
    }
    assert row["run_id"] == "11111111-2222-3333-4444-555555555555"
    assert row["pnl"] == pytest.approx(10.0)
    assert row["fees"] == pytest.approx(2.0)
