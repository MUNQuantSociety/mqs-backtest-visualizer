"""A saved strategy trades the tickers its author chose, not the default pair.

The Build tab saves a rules strategy for the ticker it charted; before this the
stored config.json always said AAPL and MSFT. No database, S3 or FMP here.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from engine.data.fmp import FMPUnavailable
from src.schemas.market_data import TickerValidation, TickerValidationResponse
from src.schemas.strategies import StrategyDraftSubmission, StrategySubmission
from src.services import strategies as service
from src.services.strategy_validation import StrategyValidationError, build_config
from src.services.strategy_validation.packaging import DEFAULT_TICKERS
from src.services.strategy_validation.template import STARTER_SOURCE

KEY = "user-tickers-test"


# --- config.json -------------------------------------------------------------

def test_config_uses_the_chosen_tickers_with_equal_weights():
    config = build_config(KEY, ["NVDA", "AMD", "TSM"])

    assert config["TICKERS"] == ["NVDA", "AMD", "TSM"]
    assert config["WEIGHTS"] == {"NVDA": 0.333333, "AMD": 0.333333, "TSM": 0.333333}
    assert config["PORTFOLIO_ID"] == KEY


@pytest.mark.parametrize("tickers", [None, []])
def test_no_tickers_keeps_the_default_pair(tickers):
    config = build_config(KEY, tickers)

    assert config["TICKERS"] == list(DEFAULT_TICKERS)
    assert config["WEIGHTS"] == {ticker: 0.5 for ticker in DEFAULT_TICKERS}


# --- the ticker check before anything is stored --------------------------------

def validation(*unknown, tickers=()):
    rows = [TickerValidation(ticker=t, status="unknown" if t in unknown else "valid") for t in tickers]
    return TickerValidationResponse(tickers=rows, unknown=list(unknown))


@pytest.fixture
def fmp_check(monkeypatch):
    check = AsyncMock(side_effect=lambda wanted: validation(tickers=wanted))
    monkeypatch.setattr(service.market_data_service, "validate_tickers", check)
    return check


def test_omitted_tickers_skip_the_check(fmp_check):
    assert asyncio.run(service._checked_universe(None)) is None
    fmp_check.assert_not_awaited()


def test_tickers_are_normalised_and_deduplicated(fmp_check):
    assert asyncio.run(service._checked_universe([" nvda", "NVDA", "amd "])) == ["NVDA", "AMD"]
    fmp_check.assert_awaited_once_with(["NVDA", "AMD"])


def test_a_symbol_fmp_does_not_know_is_one_readable_sentence(fmp_check):
    fmp_check.side_effect = lambda wanted: validation("ZZZZ", tickers=wanted)

    with pytest.raises(StrategyValidationError, match="FMP does not recognise ZZZZ. Check the ticker and try again."):
        asyncio.run(service._checked_universe(["NVDA", "ZZZZ"]))


@pytest.mark.parametrize("tickers", [[], ["not a ticker!"], [f"T{i}" for i in range(51)]])
def test_malformed_lists_are_refused_without_asking_fmp(fmp_check, tickers):
    with pytest.raises(StrategyValidationError):
        asyncio.run(service._checked_universe(tickers))
    fmp_check.assert_not_awaited()


def test_an_fmp_outage_does_not_block_saving(fmp_check):
    fmp_check.side_effect = FMPUnavailable("FMP could not be reached.")

    assert asyncio.run(service._checked_universe(["NVDA"])) == ["NVDA"]


# --- end to end through the submit path ----------------------------------------

@pytest.fixture
def isolated_submit(monkeypatch, fmp_check):
    @asynccontextmanager
    async def session_scope():
        yield object()

    create = AsyncMock()
    store = Mock(return_value=f"strategies/{KEY}/")
    begin = AsyncMock(return_value=("Validation queued", None))
    monkeypatch.setattr(service, "session_scope", session_scope)
    monkeypatch.setattr(service, "ensure_schema", AsyncMock())
    monkeypatch.setattr(service, "_generate_key", lambda name: KEY)
    monkeypatch.setattr(service, "_begin_validation", begin)
    monkeypatch.setattr(service.strategies_repo, "create_strategy", create)
    monkeypatch.setattr(service.strategy_validation, "migrate_staged_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(service.strategy_validation, "store_strategy_source", store)
    return SimpleNamespace(create=create, store=store, begin=begin)


def test_an_upload_with_tickers_is_stored_registered_and_validated_on_them(isolated_submit):
    asyncio.run(service.submit_strategy(
        StrategySubmission(name="NVDA only", source=STARTER_SOURCE, tickers=["nvda"])
    ))

    stored_config = isolated_submit.store.call_args.args[2]
    assert stored_config["TICKERS"] == ["NVDA"]
    assert isolated_submit.create.await_args.kwargs["universe"] == ["NVDA"]
    # The validation backtest runs on the same config, so on the same ticker.
    assert isolated_submit.begin.await_args.args[2]["TICKERS"] == ["NVDA"]


def test_an_unknown_ticker_stores_nothing(isolated_submit, fmp_check):
    fmp_check.side_effect = lambda wanted: validation("ZZZZ", tickers=wanted)

    with pytest.raises(StrategyValidationError):
        asyncio.run(service.submit_strategy(
            StrategySubmission(name="Typo", source=STARTER_SOURCE, tickers=["ZZZZ"])
        ))
    isolated_submit.store.assert_not_called()
    isolated_submit.create.assert_not_awaited()


def test_a_builder_draft_passes_its_tickers_through(monkeypatch):
    submit = AsyncMock(return_value="submitted")
    monkeypatch.setattr(service, "submit_strategy", submit)

    asyncio.run(service.submit_draft(StrategyDraftSubmission(
        name="Built on NVDA", body="pass", tickers=["NVDA"], rules={"buy": {}},
    )))

    assert submit.await_args.args[0].tickers == ["NVDA"]


def test_a_draft_without_tickers_keeps_todays_behaviour(monkeypatch):
    submit = AsyncMock(return_value="submitted")
    monkeypatch.setattr(service, "submit_strategy", submit)

    asyncio.run(service.submit_draft(StrategyDraftSubmission(name="Default pair", body="pass")))

    assert submit.await_args.args[0].tickers is None
