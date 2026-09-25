"""What a failed run tells the student: the engine's own words, or a bug's name."""

from datetime import date

from engine.contracts.errors import EngineError, NoMarketData
from engine.data.intraday import IntradayResolutionUnavailable
from engine.run_single import failure_message


def test_an_engine_error_reads_as_its_own_message():
    exc = IntradayResolutionUnavailable(
        "The market-data database stores 60-minute bars, so 5-minute bars cannot be built from it."
    )

    assert failure_message(exc) == (
        "The market-data database stores 60-minute bars, so 5-minute bars cannot be built from it."
    )


def test_no_market_data_names_the_window_without_the_class():
    exc = NoMarketData(["AAPL"], date(2026, 3, 1), date(2026, 3, 31))

    message = failure_message(exc)

    assert not message.startswith("NoMarketData")
    assert "AAPL" in message and "2026-03-01" in message


def test_an_unexpected_error_keeps_its_class_name_so_it_reads_as_a_bug():
    assert failure_message(KeyError("close_price")) == "KeyError: 'close_price'"


def test_an_engine_error_with_no_message_falls_back_to_its_class_name():
    assert failure_message(EngineError()) == "EngineError"
