"""Allowlisted CSV/JSON exports of the same persisted report served to the UI.

No filesystem paths are accepted and exports do not depend on ephemeral worker
artifacts. A deployment/restart therefore cannot invalidate an existing report.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any

from src.schemas.backtests import BacktestDetail

EXPORT_NAMES = ("equity.csv", "trades.csv", "metrics.csv", "report.json")


@dataclass(frozen=True)
class ReportExport:
    content: str
    media_type: str


def _csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=columns, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    # Quoting alone does not prevent spreadsheet formula execution. Only text
    # is escaped; negative numeric returns and P&L must remain numeric.
    for row in rows:
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return output.getvalue()


def _spreadsheet_safe(value: Any) -> Any:
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def export_report(detail: BacktestDetail, filename: str) -> ReportExport:
    if filename not in EXPORT_NAMES:
        raise ValueError("Unknown report export.")
    data = detail.model_dump(mode="json", by_alias=True)
    if filename == "report.json":
        return ReportExport(
            json.dumps(data, allow_nan=False, indent=2) + "\n", "application/json"
        )
    if filename == "equity.csv":
        content = _csv(["date", "equity", "benchmark"], data["equityCurve"])
    elif filename == "trades.csv":
        content = _csv(
            [
                "id",
                "symbol",
                "side",
                "entryDate",
                "exitDate",
                "entryPrice",
                "exitPrice",
                "quantity",
                "pnl",
                "returnPct",
                "fees",
            ],
            data["trades"],
        )
    else:
        unavailable = data["metrics"].get("unavailable", {})
        rows = [
            {
                "metric": key,
                "value": None if key in unavailable else value,
                "unavailableReason": unavailable.get(key, ""),
            }
            for key, value in data["metrics"].items()
            if key != "unavailable"
        ]
        rows += [
            {"metric": key, "value": data[key], "unavailableReason": ""}
            for key in ("initialCapital", "finalEquity")
        ]
        content = _csv(["metric", "value", "unavailableReason"], rows)
    return ReportExport(content, "text/csv")
