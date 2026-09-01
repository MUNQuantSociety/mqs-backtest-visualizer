"""Turning an accepted upload into something the engine can load.

An upload arrives as one file. The engine needs a directory: ``BasePortfolio``
finds its config by looking beside the file its class was defined in, so source
and config are written together and must stay together.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.integrations.strategy_store import get_strategy_store, strategy_key

logger = logging.getLogger(__name__)

# Both names match ``engine/strategies/user_loader.py``. Spelled again rather
# than imported: importing the loader drags pandas into every process that only
# wants to write a file.
SOURCE_FILENAME = "strategy.py"
CONFIG_FILENAME = "config.json"

# The frontend's upload form sends name, description, source and filename and
# nothing else, so the configuration a strategy needs is generated. Two liquid
# large caps keep a validation run short; a student who wants a different
# universe re-runs the strategy from the catalogue once it is active.
DEFAULT_TICKERS = ("AAPL", "MSFT")
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_LOOKBACK_DAYS = 30
# Uppercase because that is the engine's vocabulary: the runner keys its data
# dictionary with "MARKET_DATA", and a lower-case spelling reaches no feed.
DEFAULT_DATA_FEEDS = ("MARKET_DATA",)

# The one parameter an upload advertises to the run form. Deliberately not
# TICKERS: overriding the ticker list without also overriding WEIGHTS produces
# a run whose benchmark and risk outputs are misaligned with what it traded.
LOOKBACK_PARAM_SPEC: dict[str, Any] = {
    "key": "LOOKBACK_DAYS",
    "label": "Lookback (days)",
    "type": "integer",
    "default": DEFAULT_LOOKBACK_DAYS,
    "min": 5,
    "max": 365,
}


def build_config(strategy_key_value: str) -> dict[str, Any]:
    """The ``config.json`` stored beside an upload's source.

    Same shape as a built-in portfolio's config, because the engine reads it
    with the same code: ``BasePortfolio`` pulls PORTFOLIO_ID, TICKERS, WEIGHTS,
    INTERVAL, LOOKBACK_DAYS and DATA_FEEDS straight out of this dictionary.
    """
    weight = round(1.0 / len(DEFAULT_TICKERS), 6)
    return {
        "PORTFOLIO_ID": strategy_key_value,
        "TICKERS": list(DEFAULT_TICKERS),
        "WEIGHTS": {ticker: weight for ticker in DEFAULT_TICKERS},
        "INTERVAL": DEFAULT_INTERVAL_SECONDS,
        "LOOKBACK_DAYS": DEFAULT_LOOKBACK_DAYS,
        "DATA_FEEDS": list(DEFAULT_DATA_FEEDS),
    }


def parameter_specs() -> list[dict[str, Any]]:
    """The catalogue's parameter form for an upload."""
    return [dict(LOOKBACK_PARAM_SPEC)]


def store_strategy_source(key: str, source: str, config: dict[str, Any]) -> str:
    """Write an upload into the strategy store and return its storage key.

    Source and config go in together because the engine needs them together:
    ``BasePortfolio`` finds its config by looking beside the file its class was
    defined in, so a key holding only ``strategy.py`` materializes into a
    directory the engine cannot configure.
    """
    storage = strategy_key(key)
    store = get_strategy_store()
    store.put(storage, SOURCE_FILENAME, source)
    store.put(storage, CONFIG_FILENAME, json.dumps(config, indent=2) + "\n")
    return storage


def discard_stored_source(key: str) -> None:
    """Remove everything stored for a strategy. Missing is not an error."""
    try:
        get_strategy_store().delete(strategy_key(key))
    except Exception:
        # Deleting the registry row is what the caller asked for and it has
        # already happened; an object left in the store is unreachable, not
        # broken.
        logger.exception("Stored source for strategy %s could not be removed", key)


