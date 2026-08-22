"""The vendored MQS backtest engine.

This package is a *copy* of the backtest engine that lives in the MQSMaster
trading repository, not a dependency on it. It was vendored so this repo can
modify engine internals (progress reporting, cancellation, structured results)
without touching the trading system — see ``engine/VENDORED_FROM`` for the
exact upstream commit and ``# VISUALIZER:`` comments for every local edit.

Nothing under ``engine/`` may import from ``src/``: the engine has to stay
runnable on its own, and the dependency arrow points one way (``src`` →
``engine``) so a worker process can import it without dragging in FastAPI.
"""

from __future__ import annotations

# Kept in lockstep with engine/VENDORED_FROM (full SHA) by hand — it is written
# to every run row as ``engine_version``, so a result can always be traced back
# to the code that produced it.
ENGINE_VERSION = "vendored-31d9570"

__all__ = ["ENGINE_VERSION"]
