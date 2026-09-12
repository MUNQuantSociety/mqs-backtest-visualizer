"""Upload guardrails and validation-by-backtest for user strategies.

A student uploads a ``.py`` file. Nothing about it is trusted, and the only way
to know whether it works is to run it. Three steps, one module each:

* :mod:`.scanning` reads the source without executing it. Refuses what must not
  be stored, and reports compatibility for the editor's pre-flight check.
* :mod:`.packaging` writes an accepted upload into the strategy store as the
  source-plus-config pair the engine can load.
* :mod:`.runs` queues the ordinary backtest that proves it works, and marks the
  strategy active or failed on the result.

This file is the public surface. Callers import from here, so the split above
can change without touching them.

SECURITY: validating a strategy means executing user-supplied Python in a
worker holding admin credentials to the production database. The scan is a
speed bump against accidents, not a boundary. See :mod:`.scanning`.
"""

from __future__ import annotations

from src.services.strategy_validation.packaging import (
    CONFIG_FILENAME,
    SOURCE_FILENAME,
    build_config,
    discard_stored_source,
    parameter_specs,
    store_strategy_source,
)
from src.services.strategy_validation.runs import (
    ValidationStartError,
    mark_validation_unstarted,
    migrate_staged_sources,
    start_validation,
    validation_window,
)
from src.services.strategy_validation.scanning import (
    ALLOWED_IMPORT_ROOTS,
    BASE_CLASS_NAME,
    CompatibilityIssue,
    CompatibilityReport,
    SourceScan,
    StrategyValidationError,
    check_compatibility,
    scan_source,
)

__all__ = [
    "ALLOWED_IMPORT_ROOTS",
    "BASE_CLASS_NAME",
    "CONFIG_FILENAME",
    "CompatibilityIssue",
    "CompatibilityReport",
    "SOURCE_FILENAME",
    "SourceScan",
    "StrategyValidationError",
    "ValidationStartError",
    "build_config",
    "check_compatibility",
    "discard_stored_source",
    "mark_validation_unstarted",
    "migrate_staged_sources",
    "parameter_specs",
    "scan_source",
    "start_validation",
    "validation_window",
]
