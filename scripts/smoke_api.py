"""Run a real backtest through the API (or Vite proxy) and save verified exports.

Explicitly creates a run. It does not delete any strategy/run or alter market
data. Use --base-url http://127.0.0.1:5173/api to exercise the frontend proxy.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.schemas.backtests import BacktestDetail, BacktestSummary
from src.services.report_exports import EXPORT_NAMES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--name", default="API integration verification")
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    began = time.monotonic()
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{base}/backtests",
            json={
                "name": args.name,
                "strategyKey": args.strategy,
                "startDate": args.start,
                "endDate": args.end,
                "initialCapital": args.capital,
                "mode": "event",
            },
        )
        response.raise_for_status()
        if response.status_code != 202:
            raise RuntimeError("Run endpoint did not accept with HTTP 202")
        accepted = BacktestSummary.model_validate(response.json())
        print(f"HTTP 202 run={accepted.id} strategy={accepted.strategy_id}", flush=True)
        last_progress = None
        while time.monotonic() - began < args.timeout:
            response = client.get(f"{base}/backtests/{accepted.id}")
            response.raise_for_status()
            detail = BacktestDetail.model_validate(response.json())
            progress = (detail.status.value, detail.progress_pct)
            if progress != last_progress:
                print(f"status={progress[0]} progress={progress[1]}", flush=True)
                last_progress = progress
            if detail.status.value == "failed":
                raise RuntimeError(
                    f"Backtest {accepted.id} failed: {detail.error_message}"
                )
            if detail.status.value == "completed":
                break
            time.sleep(1)
        else:
            raise TimeoutError(
                f"Run {accepted.id} still pending; inspect its status (not deleted/cancelled)."
            )
        if (
            not detail.equity_curve
            or abs(detail.final_equity - detail.equity_curve[-1].equity) > 1e-6
        ):
            raise RuntimeError("Final equity/curve mismatch")
        directory = ROOT / ".artifacts" / accepted.id / "report"
        directory.mkdir(parents=True, exist_ok=True)
        for filename in EXPORT_NAMES:
            response = client.get(f"{base}/backtests/{accepted.id}/exports/{filename}")
            response.raise_for_status()
            if filename == "report.json":
                if response.json() != detail.model_dump(mode="json", by_alias=True):
                    raise RuntimeError("JSON export differs from detail")
            elif filename == "equity.csv":
                rows = list(csv.DictReader(io.StringIO(response.text)))
                if len(rows) != len(detail.equity_curve):
                    raise RuntimeError("CSV equity length differs from detail")
                for row, point in zip(rows, detail.equity_curve):
                    benchmark = float(row["benchmark"]) if row["benchmark"] else None
                    if (
                        row["date"] != point.date
                        or float(row["equity"]) != point.equity
                        or benchmark != point.benchmark
                    ):
                        raise RuntimeError("CSV equity/benchmark differs from detail")
            (directory / filename).write_text(
                response.text, encoding="utf-8", newline="\n"
            )
        print(
            json.dumps(
                {
                    "runId": accepted.id,
                    "elapsedSeconds": round(time.monotonic() - began, 2),
                    "equityPoints": len(detail.equity_curve),
                    "benchmarkPoints": sum(
                        p.benchmark is not None for p in detail.equity_curve
                    ),
                    "tradeLots": len(detail.trades),
                    "closedTrades": detail.metrics.total_trades,
                    "finalEquity": detail.final_equity,
                    "exports": str(directory),
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
