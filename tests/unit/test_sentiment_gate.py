"""Engine sentiment gate: point-in-time scoring and the long-entry cap."""

from datetime import datetime, timedelta, timezone

import pytest

from engine.core.executor import BacktestExecutor
from engine.core.sentiment_gate import SentimentGate, available_at

# 15:00 UTC on a published_at; available five hours later, at 20:00 UTC.
PUBLISHED = datetime(2026, 7, 14, 15, 0)
AVAILABLE = datetime(2026, 7, 14, 20, 0)


def _gate(threshold=-0.25, articles=None):
    return SentimentGate(threshold=threshold, articles=articles or {"AAPL": []})


def test_an_article_counts_five_hours_after_publication():
    assert available_at(PUBLISHED) == AVAILABLE


def test_a_midnight_article_counts_five_hours_after_publication():
    assert available_at(datetime(2026, 7, 14)) == datetime(2026, 7, 14, 5, 0)


def test_an_article_is_excluded_at_the_exact_moment_it_becomes_available():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)]})

    assert gate.score_at("AAPL", AVAILABLE) == 0.0


def test_an_article_counts_just_after_it_becomes_available():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)]})

    assert gate.score_at("AAPL", AVAILABLE + timedelta(seconds=1)) == pytest.approx(-0.8)


def test_an_article_available_exactly_seven_days_ago_still_counts():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)]})

    assert gate.score_at("AAPL", AVAILABLE + timedelta(days=7)) == pytest.approx(-0.8)


def test_an_article_older_than_seven_days_drops_out():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)]})

    assert gate.score_at("AAPL", AVAILABLE + timedelta(days=7, seconds=1)) == 0.0


def test_the_score_is_the_mean_of_articles_in_the_window():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8), (PUBLISHED + timedelta(hours=1), 0.2)]})

    assert gate.score_at("AAPL", AVAILABLE + timedelta(hours=2)) == pytest.approx(-0.3)


def test_a_timezone_aware_bar_time_is_compared_in_utc():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)]})
    new_york_after = datetime(2026, 7, 14, 16, 30, tzinfo=timezone(timedelta(hours=-4)))

    assert gate.score_at("AAPL", new_york_after) == pytest.approx(-0.8)


def test_a_ticker_with_no_articles_scores_neutral():
    assert _gate().score_at("MSFT", AVAILABLE) == 0.0


def test_a_neutral_score_never_blocks_even_at_threshold_zero():
    assert not _gate(threshold=0.0).blocks_long_entry("AAPL", AVAILABLE)


def test_a_score_below_the_threshold_blocks():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.3)]})

    assert gate.blocks_long_entry("AAPL", AVAILABLE + timedelta(hours=1))


def test_a_score_equal_to_the_threshold_does_not_block():
    gate = _gate(threshold=-0.3, articles={"AAPL": [(PUBLISHED, -0.3)]})

    assert not gate.blocks_long_entry("AAPL", AVAILABLE + timedelta(hours=1))


@pytest.mark.parametrize("threshold", [0.1, -1.1])
def test_a_threshold_outside_minus_one_to_zero_is_rejected(threshold):
    with pytest.raises(ValueError, match="threshold"):
        _gate(threshold=threshold)


def test_report_includes_coverage_and_blocked_count():
    gate = _gate(articles={"AAPL": [(PUBLISHED, -0.8)], "MSFT": []})
    gate.record_block()

    assert gate.report() == {
        "enabled": True,
        "threshold": -0.25,
        "window": "7d",
        "blockedEntryCount": 1,
        "coverage": {
            "AAPL": {"articleCount": 1, "firstAvailable": "2026-07-14T20:00:00+00:00"},
            "MSFT": {"articleCount": 0, "firstAvailable": None},
        },
    }


# --- Executor: the cap on long exposure ------------------------------------

PRICE = 100.0
BAR = AVAILABLE + timedelta(hours=1)
NEGATIVE = {"AAPL": [(PUBLISHED, -0.9)]}


def _executor(gate, quantity=0.0):
    executor = BacktestExecutor(initial_capital=100_000.0, tickers=["AAPL"], sentiment_gate=gate)
    executor.update_price("AAPL", PRICE)
    executor.positions["AAPL"] = quantity
    executor.cash -= quantity * PRICE
    executor.current_time = BAR
    return executor


def _size(executor, target_weight):
    return executor.default_trade_size(
        portfolio_id="p",
        signal_type="BUY",
        ticker="AAPL",
        arrival_price=PRICE,
        confidence=1.0,
        cash=executor.cash,
        positions=None,
        port_notional=100_000.0,
        ticker_weight=0.0,
        target_weight=target_weight,
    )


def test_a_new_long_is_blocked():
    assert _size(_executor(_gate(articles=NEGATIVE)), 0.5).quantity == 0


def test_adding_to_a_long_is_blocked():
    assert _size(_executor(_gate(articles=NEGATIVE), quantity=100), 0.5).quantity == 0


def test_a_blocked_entry_is_counted():
    gate = _gate(articles=NEGATIVE)

    _size(_executor(gate), 0.5)

    assert gate.blocked_entry_count == 1


def test_trimming_a_long_is_allowed():
    sizing = _size(_executor(_gate(articles=NEGATIVE), quantity=300), 0.1)

    assert sizing.quantity > 0
    assert sizing.desired_notional < 0


def test_covering_a_short_stops_at_flat():
    sizing = _size(_executor(_gate(articles=NEGATIVE), quantity=-100), 0.5)

    assert sizing.quantity == 100
    assert sizing.desired_notional > 0


def test_a_long_entry_is_allowed_when_news_is_above_the_threshold():
    gate = _gate(articles={"AAPL": [(PUBLISHED, 0.4)]})

    assert _size(_executor(gate), 0.5).quantity > 0


def test_an_ungated_executor_sizes_as_before():
    assert _size(_executor(None), 0.5).quantity == 500
