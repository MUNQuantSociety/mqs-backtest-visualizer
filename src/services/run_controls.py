"""Validate run-form controls separately from strategy-specific parameters.

The frontend's existing wire format places both in ``params``. Only the
explicit keys here are execution controls; unknown strategy keys still fail.
"""

from __future__ import annotations

import math
import re
from typing import Any

from engine.data.bar_interval import is_intraday
from src.core.config import settings

# The run form's slider range; mirrors engine/core/sentiment_gate.py.
SENTIMENT_THRESHOLD_MIN = -1.0
SENTIMENT_THRESHOLD_MAX = 0.0

CONTROL_KEYS = frozenset(
    {
        "universe",
        "slippageBps",
        "commissionPerShare",
        "signals",
        "sentimentGate",
        "barIntervalSeconds",
    }
)

# The API's ``timeframe`` label for each bar size the run form offers.
_TIMEFRAME_LABELS = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 86400: "1d"}


def timeframe_label(params: dict[str, Any]) -> str:
    """The run's bar size as a ``timeframe`` label; ``"1d"`` when unset.

    Runs submitted before the bar-interval control existed carry no key and
    were daily, which is why daily is the fallback rather than an error.
    """
    seconds = params.get("barIntervalSeconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        return "1d"
    return _TIMEFRAME_LABELS.get(seconds, "1d")


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

    if "barIntervalSeconds" in raw:
        controls.update(_bar_interval_controls(raw["barIntervalSeconds"], mode))

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


def _bar_interval_controls(value: Any, mode: str) -> dict[str, int]:
    """Validate the bar size and return the controls and engine overlay it implies.

    A daily bar is recorded but overlays nothing, so the strategy runs exactly
    as it did before the control existed. An intraday bar sets the engine's
    ``BAR_INTERVAL_SECONDS`` and makes the strategy decide on every bar
    (``INTERVAL`` 0). Setting ``INTERVAL`` to the bar length instead would skip
    the session's short closing bar (15:30–16:00 on hourly bars), because the
    runner drops any bar closer than ``INTERVAL`` to the previous decision.

    Raises:
        ValueError: An unsupported size, or intraday bars in fast mode.
    """
    intraday = is_intraday(value)
    seconds = int(value)
    if not intraday:
        return {"barIntervalSeconds": seconds}
    if mode == "fast":
        raise ValueError(
            "Intraday bars require event mode; use event mode or a 1-day bar interval."
        )
    return {"barIntervalSeconds": seconds, "BAR_INTERVAL_SECONDS": seconds, "INTERVAL": 0}
