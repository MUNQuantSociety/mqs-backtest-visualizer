"""Create the ``app`` schema and seed the built-in strategy registry.

    venv/Scripts/python.exe scripts/seed_strategies.py

Idempotent: re-running upserts the four built-ins and leaves user-uploaded
rows, run history, and ``created_at`` alone. Run it after every schema change
and on any fresh database.

The registry rows below are the vendored MQSMaster portfolios. ``universe`` and
``param_specs`` come from each portfolio's ``config.json``; when the engine has
already been vendored into ``engine/strategies/<key>/config.json`` that file
wins, so the catalogue can never drift from the code that actually runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from src.db.engine import create_sync_engine, sync_session_scope  # noqa: E402
from src.db.init import init_database  # noqa: E402
from src.models import Strategy  # noqa: E402

# Where a vendored strategy's own config.json lives once lane B has copied it.
ENGINE_STRATEGY_ROOT = REPO_ROOT / "engine" / "strategies"


def _lookback_spec(default: int) -> dict[str, Any]:
    """Trailing history the strategy warms its indicators on."""
    return {
        "key": "LOOKBACK_DAYS",
        "label": "Lookback (days)",
        "type": "integer",
        "default": default,
        "min": 5,
        "max": 365,
    }


def _interval_spec(default: int) -> dict[str, Any]:
    """``INTERVAL`` is seconds — the runner builds ``Timedelta(seconds=...)``."""
    return {
        "key": "INTERVAL",
        "label": "Poll interval (seconds)",
        "type": "integer",
        "default": default,
        "min": 60,
        "max": 86400,
    }


# Fallback copies of each portfolio's config.json values, recorded here so a
# fresh clone can seed before the engine is vendored. Keys and defaults are
# verbatim from MQSMaster/src/portfolios/<key>/config.json.
_CONFIG_FALLBACKS: dict[str, dict[str, Any]] = {
    "portfolio_1": {
        "TICKERS": ["AAPL", "TSLA", "AMD", "MSFT", "NVDA"],
        "LOOKBACK_DAYS": 90,
        "INTERVAL": 60,
    },
    "portfolio_2": {
        "TICKERS": ["AAPL", "TSLA", "AMD", "MSFT", "NVDA"],
        "LOOKBACK_DAYS": 90,
        "INTERVAL": 60,
    },
    "portfolio_3": {
        "TICKERS": [
            "AAPL", "TSLA", "AMZN", "MSFT", "NVDA", "JPM", "XOM",
            "UNH", "CAT", "WMT", "TLT", "GLD", "^VIX",
        ],
        "LOOKBACK_DAYS": 90,
        "INTERVAL": 60,
    },
    "portfolio_dummy": {
        "TICKERS": ["AAPL", "TSLA", "NVDA", "MSFT"],
        "LOOKBACK_DAYS": 50,
        "INTERVAL": 3600,
    },
}

# Everything that is not in config.json: the human-facing catalogue copy and
# the import path the run pipeline builds the class from.
_BUILTINS: list[dict[str, Any]] = [
    {
        "key": "portfolio_1",
        "name": "Volatility Momentum",
        "class_path": "engine.strategies.portfolio_1.strategy.VolMomentum",
        "description": (
            "Buys names whose 20-period rate of change clears their own annualised "
            "volatility band and sells them when it breaks the other way, holding "
            "cash when the book runs low."
        ),
        "tags": ["momentum", "volatility"],
        "enabled": True,
    },
    {
        "key": "portfolio_2",
        "name": "Multi-Indicator Momentum",
        "class_path": "engine.strategies.portfolio_2.strategy.MomentumStrategy",
        "description": (
            "Combines a fast/slow moving-average crossover with RMI, RSI and a "
            "displaced moving average, trading only when the indicators agree."
        ),
        "tags": ["momentum", "crossover", "multi-indicator"],
        "enabled": True,
    },
    {
        "key": "portfolio_3",
        "name": "Regime Adaptive",
        "class_path": "engine.strategies.portfolio_3.strategy.RegimeAdaptiveStrategy",
        "description": (
            "Classifies the market into trend, reversal and range regimes across a "
            "13-name cross-asset universe and switches signal logic per regime, with "
            "ATR-based stops and confidence-weighted sizing."
        ),
        "tags": ["regime", "adaptive", "cross-asset"],
        "enabled": True,
    },
    {
        "key": "portfolio_dummy",
        "name": "Crossover + RMI (test harness)",
        "class_path": "engine.strategies.portfolio_dummy.strategy.CrossoverRmiStrategy",
        "description": (
            "A deliberately small SMA-crossover-plus-RMI strategy used to exercise "
            "the run pipeline end to end. Disabled in the catalogue: it exists to "
            "prove the plumbing, not to be traded."
        ),
        "tags": ["test", "crossover"],
        # Not offered to students — it is the smoke test for the pipeline.
        "enabled": False,
    },
]


def load_config(key: str) -> dict[str, Any]:
    """Vendored ``config.json`` if it exists, otherwise the recorded fallback."""
    vendored = ENGINE_STRATEGY_ROOT / key / "config.json"
    if vendored.is_file():
        try:
            return json.loads(vendored.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # A half-written file during vendoring must not silently seed a
            # wrong universe — say so and use the known-good values.
            print(f"  ! {vendored} is not valid JSON ({exc}); using recorded values")
    return _CONFIG_FALLBACKS[key]


def build_rows() -> list[dict[str, Any]]:
    """Registry rows, ready to upsert."""
    rows: list[dict[str, Any]] = []
    for builtin in _BUILTINS:
        config = load_config(builtin["key"])
        fallback = _CONFIG_FALLBACKS[builtin["key"]]
        rows.append(
            {
                "key": builtin["key"],
                "name": builtin["name"],
                "description": builtin["description"],
                "tags": builtin["tags"],
                "universe": list(config.get("TICKERS", fallback["TICKERS"])),
                "param_specs": [
                    _lookback_spec(
                        int(config.get("LOOKBACK_DAYS", fallback["LOOKBACK_DAYS"]))
                    ),
                    _interval_spec(int(config.get("INTERVAL", fallback["INTERVAL"]))),
                ],
                "kind": "builtin",
                "class_path": builtin["class_path"],
                "storage_key": None,
                "status": "active",
                "enabled": builtin["enabled"],
            }
        )
    return rows


def seed() -> list[dict[str, Any]]:
    """Create the schema if needed, then upsert every built-in. Returns the rows."""
    rows = build_rows()
    engine = create_sync_engine()
    try:
        init_database(engine)
        with sync_session_scope(engine) as session:
            statement = insert(Strategy).values(rows)
            # Catalogue copy and config are owned by this script; run history,
            # created_at, and a user strategy's validation_run_id are not.
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Strategy.key],
                    set_={
                        column: statement.excluded[column]
                        for column in (
                            "name",
                            "description",
                            "tags",
                            "universe",
                            "param_specs",
                            "kind",
                            "class_path",
                            "status",
                            "enabled",
                        )
                    },
                )
            )
    finally:
        engine.dispose()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rows that would be written and touch no database",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(build_rows(), indent=2))
        return 0

    rows = seed()
    print(f"Seeded {len(rows)} strategies into app.strategies:")
    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        print(
            f"  {row['key']:<16} {row['name']:<34} {state:<8} "
            f"{len(row['universe'])} tickers"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
