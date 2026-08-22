"""Prove the vendored engine can talk to the real market_data table.

Run it after touching anything in ``engine/data/`` or ``engine/core/utils.py``:

    venv/Scripts/python.exe scripts/smoke_engine.py

It builds ``portfolio_dummy`` against the live database through
``EngineDBAdapter`` and pulls a short window of daily bars. A non-empty frame
means the whole seam works end to end — credentials, TLS mode, the
DISTINCT ON query, the timezone filter, the parquet cache, and the indicator
warm-up that runs during construction. Exit code 1 means it does not.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import ENGINE_VERSION  # noqa: E402
from engine.core.utils import fetch_historical_data  # noqa: E402
from engine.data.db_adapter import EngineDBAdapter  # noqa: E402
from engine.strategies.portfolio_dummy.strategy import (  # noqa: E402
    CrossoverRmiStrategy,
)


def latest_bar_date(adapter: EngineDBAdapter, ticker: str) -> date | None:
    """Newest calendar date ``market_data`` holds for one ticker.

    ``ORDER BY timestamp DESC LIMIT 1`` rather than ``MAX(...)`` so the query
    walks the index instead of the (very large) table.
    """
    result = adapter.execute_query(
        "SELECT date FROM market_data WHERE ticker = %s "
        "ORDER BY timestamp DESC LIMIT 1",
        [ticker],
        fetch=True,
    )
    if result.get("status") != "success" or not result.get("data"):
        return None
    return result["data"][0]["date"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="calendar days back from --end (14 ~ 10 trading days)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="window end as YYYY-MM-DD (default: the newest bar in market_data)",
    )
    parser.add_argument("--verbose", action="store_true", help="engine debug logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config_path = (
        REPO_ROOT / "engine" / "strategies" / "portfolio_dummy" / "config.json"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))

    print(f"engine version : {ENGINE_VERSION}")
    print(f"tickers        : {', '.join(config['TICKERS'])}")

    adapter = EngineDBAdapter()
    try:
        # Default to the newest bar rather than to today: market_data is
        # backfilled in batches, so "yesterday" is routinely past the end of
        # coverage and would make a healthy engine look broken.
        end = (
            date.fromisoformat(args.end)
            if args.end
            else latest_bar_date(adapter, config["TICKERS"][0])
        )
        if end is None:
            print("FAIL: market_data holds no bars for this strategy's universe.")
            return 1
        start = end - timedelta(days=args.days)
        print(f"window         : {start} -> {end}")

        # Constructing the strategy warms every indicator from the database,
        # so a successful build already exercises execute_query.
        portfolio = CrossoverRmiStrategy(
            db_connector=adapter,
            executor=None,
            config_dict=config,
            backtest_start_date=None,
        )
        frame = fetch_historical_data(portfolio, start, end)
    finally:
        adapter.close()

    if frame.empty:
        print("FAIL: fetch_historical_data returned an empty DataFrame.")
        return 1

    print(f"shape          : {frame.shape}")
    print(f"columns        : {list(frame.columns)}")
    print(f"tickers seen   : {sorted(frame['ticker'].unique())}")
    print(f"timestamp span : {frame['timestamp'].min()} -> {frame['timestamp'].max()}")
    print(frame.head().to_string(index=False))
    print("OK: non-empty market data returned from public.market_data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
