"""Fail CI when disposable-database coverage did not actually execute."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check_report(path: Path) -> int:
    """Check testcase outcomes, without double-counting nested suite totals."""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("PostgreSQL fixture suite executed no tests")
    if any(case.find(status) is not None for case in cases
           for status in ("skipped", "failure", "error")):
        raise ValueError("PostgreSQL fixture suite must pass with no skips")
    return len(cases)


if __name__ == "__main__":
    try:
        count = check_report(Path(sys.argv[1]))
    except (IndexError, OSError, ValueError, ET.ParseError) as exc:
        raise SystemExit(f"CI test report rejected: {exc}") from exc
    print(f"Verified {count} PostgreSQL fixture tests passed without skips")
