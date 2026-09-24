"""Validate run-form controls separately from strategy-specific parameters.

The frontend's existing wire format places both in ``params``. Only the
explicit keys here are execution controls; unknown strategy keys still fail.
"""

from __future__ import annotations

import math
import re
from typing import Any

from src.core.config import settings

# The run form's slider range; mirrors engine/core/sentiment_gate.py.
SENTIMENT_THRESHOLD_MIN = -1.0
SENTIMENT_THRESHOLD_MAX = 0.0

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
        gate = _validated_sentiment_gate(raw["sentimentGate"], mode)
        if gate is not None:
            controls["sentimentGate"] = gate
    return params, controls, universe


def _validated_sentiment_gate(raw: Any, mode: str) -> dict[str, Any] | None:
    """The gate to store with the run, or None when it is off.

    Refused up front rather than inside the worker: fast mode has no orders to
    gate, and without the live news database the run could only fail later or,
    worse, run ungated while the report said otherwise.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
        raise ValueError("sentimentGate must be an object with a boolean 'enabled'.")
    if not raw["enabled"]:
        return None
    threshold = raw.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not SENTIMENT_THRESHOLD_MIN <= threshold <= SENTIMENT_THRESHOLD_MAX
    ):
        raise ValueError(
            f"sentimentGate threshold must be a number from {SENTIMENT_THRESHOLD_MIN:g} "
            f"to {SENTIMENT_THRESHOLD_MAX:g}."
        )
    if mode == "fast":
        raise ValueError("The sentiment gate requires event mode.")
    if not settings.news_database_configured:
        raise ValueError(
            "The sentiment gate needs the live news database, which this server "
            "has not been configured to reach."
        )
    return {"enabled": True, "threshold": float(threshold)}
