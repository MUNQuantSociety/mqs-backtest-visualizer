"""Indicator math behind GET /indicators: textbook values and look-back limits."""

from datetime import datetime, timedelta

import pytest

from src.services import technical_indicators as ta

WINDOW_END = datetime(2026, 7, 15, 20, 0)


def test_rsi_is_100_when_every_close_rises():
    assert ta.relative_strength_index([float(n) for n in range(30)]) == 100.0


def test_rsi_is_0_when_every_close_falls():
    assert ta.relative_strength_index([float(n) for n in range(30, 0, -1)]) == 0.0


def test_rsi_is_neutral_for_a_flat_series():
    assert ta.relative_strength_index([10.0] * 15) == 50.0


def test_rsi_is_50_when_gains_and_losses_balance():
    closes = [10.0 + (n % 2) for n in range(15)]

    assert ta.relative_strength_index(closes) == pytest.approx(50.0)


def test_rsi_rejects_fewer_than_period_plus_one_closes():
    with pytest.raises(ValueError, match="RSI"):
        ta.relative_strength_index([1.0] * 14)


def test_macd_histogram_is_zero_for_a_flat_series():
    assert ta.macd_histogram([50.0] * 60) == pytest.approx(0.0)


def test_macd_histogram_is_zero_for_a_straight_line():
    # A linear trend gives a constant MACD line, which its signal line matches.
    assert ta.macd_histogram([100.0 + n for n in range(80)]) == pytest.approx(0.0, abs=1e-9)


def test_macd_histogram_is_positive_when_the_trend_accelerates():
    assert ta.macd_histogram([100.0 + 0.05 * n * n for n in range(80)]) > 0


def test_macd_histogram_rejects_too_short_a_series():
    with pytest.raises(ValueError, match="MACD"):
        ta.macd_histogram([1.0] * 33)


def test_sma_regime_is_above_when_recent_closes_lift_the_fast_average():
    assert ta.sma_regime([1.0] * 150 + [2.0] * 50) == "above"


def test_sma_regime_is_below_when_recent_closes_drag_the_fast_average():
    assert ta.sma_regime([2.0] * 150 + [1.0] * 50) == "below"


def test_sma_regime_counts_a_tie_as_below():
    assert ta.sma_regime([5.0] * 200) == "below"


def test_sma_regime_rejects_fewer_than_200_closes():
    with pytest.raises(ValueError, match="SMA\\(200\\)"):
        ta.sma_regime([1.0] * 199)


def test_momentum_is_the_20_session_return_as_a_ratio():
    assert ta.momentum([100.0] * 20 + [110.0]) == pytest.approx(0.10)


def test_momentum_rejects_a_zero_base_close():
    with pytest.raises(ValueError, match="zero"):
        ta.momentum([0.0] + [1.0] * 20)


def test_momentum_rejects_fewer_than_21_closes():
    with pytest.raises(ValueError, match="momentum"):
        ta.momentum([1.0] * 20)


def test_sentiment_is_the_mean_of_articles_in_the_last_7_days():
    articles = [(WINDOW_END - timedelta(days=1), 0.6), (WINDOW_END - timedelta(days=6), 0.2)]

    sentiment, _ = ta.sentiment_window_scores(articles, WINDOW_END)

    assert sentiment == pytest.approx(0.4)


def test_sentiment_delta_is_change_against_the_prior_7_days():
    articles = [(WINDOW_END - timedelta(days=1), 0.5), (WINDOW_END - timedelta(days=10), -0.1)]

    _, delta = ta.sentiment_window_scores(articles, WINDOW_END)

    assert delta == pytest.approx(0.6)


def test_sentiment_is_neutral_with_no_articles():
    assert ta.sentiment_window_scores([], WINDOW_END) == (0.0, 0.0)


def test_sentiment_ignores_articles_after_the_window_end():
    articles = [(WINDOW_END + timedelta(minutes=1), -0.9), (WINDOW_END, 0.3)]

    sentiment, _ = ta.sentiment_window_scores(articles, WINDOW_END)

    assert sentiment == pytest.approx(0.3)


def test_sentiment_counts_an_article_exactly_7_days_old_in_the_prior_window():
    articles = [(WINDOW_END - timedelta(days=7), 0.8)]

    assert ta.sentiment_window_scores(articles, WINDOW_END) == (0.0, pytest.approx(-0.8))
