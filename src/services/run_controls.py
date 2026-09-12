"""Validate run-form controls separately from strategy-specific parameters.

The frontend's existing wire format places both in ``params``. Only the
explicit keys here are execution controls; unknown strategy keys still fail.
"""

from __future__ import annotations

import math
import re
from typing import Any

CONTROL_KEYS = frozenset(
    {"universe", "slippageBps", "commissionPerShare", "signals", "sentimentGate"}
)


def split_controls(
    raw: dict[str, Any], default_universe: list[str], mode: str
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Return strategy parameters, persisted execution options, and tickers.

    An explicitly changed universe gets equal weights. Leaving it unchanged
    preserves the strategy's configured weights and all other engine defaults.
    """
    params = {key: value for key, value in raw.items() if key not in CONTROL_KEYS}
    controls: dict[str, Any] = {}
    universe = list(default_universe)
    if "universe" in raw:
        value = raw["universe"]
        if not isinstance(value, list) or not 1 <= len(value) <= 50:
            raise ValueError("universe must contain between 1 and 50 ticker symbols.")
        if any(not isinstance(ticker, str) for ticker in value):
            raise ValueError("Every universe ticker must be a string.")
        universe = [ticker.strip().upper() for ticker in value]
        if any(
            not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}", ticker)
            for ticker in universe
        ):
            raise ValueError("universe contains an invalid ticker symbol.")
        if len(set(universe)) != len(universe):
            raise ValueError("universe must not contain duplicate tickers.")
        controls["universe"] = universe
        if set(universe) != set(default_universe):
            controls["TICKERS"] = universe
            controls["WEIGHTS"] = {ticker: 1.0 / len(universe) for ticker in universe}

    for key, maximum in (("slippageBps", 1000.0), ("commissionPerShare", 100.0)):
        if key not in raw:
            continue
        value = raw[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= maximum
        ):
            raise ValueError(f"{key} must be a finite number from 0 to {maximum:g}.")
        controls[key] = float(value)
    if mode == "fast" and controls.get("commissionPerShare", 0) != 0:
        raise ValueError(
            "Per-share commission requires event mode; use event mode or set commission to zero."
        )

    if "signals" in raw and raw["signals"] != []:
        raise ValueError(
            "Signal overrides are not supported. Indicators are defined by the strategy code."
        )
    if "sentimentGate" in raw:
        gate = raw["sentimentGate"]
        if not isinstance(gate, dict) or gate.get("enabled") is not False:
            raise ValueError("The sentiment gate is not supported; keep it disabled.")
    return params, controls, universe
