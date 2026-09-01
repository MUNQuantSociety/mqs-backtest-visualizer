"""Repair the run table after the server died with work in flight.

A run's status is a claim about a process that is supposed to be alive. When
the API restarts — a deploy, a crash, ``uvicorn --reload`` noticing an edit —
every worker process dies with it, and the rows they had claimed keep saying
``running`` forever. The frontend polls one of those rows until the student
gives up.

So startup begins by telling the truth about the previous startup's work:

* a run left ``running`` **whose worker has stopped heartbeating** had its
  process taken away and is marked ``failed`` — and if it was validating an
  uploaded strategy, that strategy is given the same verdict, because nothing
  else will ever move it out of ``validating``. A run that is still beating
  belongs to a worker that is alive — another API instance, a rolling deploy,
  a second test module in the same process — and is left alone;
* a run left ``queued`` was never claimed by anything, and its job is still
  perfectly runnable — it is handed back to the pool.

Both are synchronous (worker-side sync engine) and both are idempotent, so the
lifespan can call them on every boot without special-casing the first one.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, Row, func, or_, select, update

from src.core.config import settings
from src.db.engine import create_sync_engine
from src.db.init import init_database
from src.models import BacktestRun

logger = logging.getLogger(__name__)

_RUNS = BacktestRun.__table__

INTERRUPTED_MESSAGE = "Interrupted by server restart"

# A restart with a huge queued backlog should not fire every one of them into
# the pool at once — the pool would hold the futures anyway, but the log line
# and the memory are worth bounding. Anything past this stays queued and is
# picked up by the next restart or resubmitted by hand.
MAX_REQUEUED_RUNS = 100


def _stale_cutoff() -> datetime:
    """Beats older than this mean the worker is gone."""
    return datetime.now(timezone.utc) - timedelta(
        seconds=float(settings.run_heartbeat_stale_seconds)
    )


def reconcile_interrupted_runs(engine: Engine | None = None) -> int:
    """Fail every ``running`` run whose worker is no longer beating. Returns how many.

    The predicate is the heartbeat, not the status. ``status='running'`` only
    says a worker *claimed* the row; whether that worker still exists is what
    ``heartbeat_at`` answers, and a boot cannot know it any other way — the
    dead worker was in a previous process, and a live one may be in another
    instance entirely. A NULL beat is treated as stale: it means the row was
    claimed before the column existed, by a process this deploy replaced.

    Safe on a healthy boot: nothing is ``running`` before the first job is
    claimed, so this matches zero rows. It must run *before* the pool starts
    accepting work, or it would race the runs it is about to start.

    Runs that were validating an upload also settle the strategy they were
    proving — see :func:`_settle_interrupted_validations`.
    """
    owned = engine is None
    engine = engine or create_sync_engine()
    try:
        init_database(engine)
        cutoff = _stale_cutoff()
        with engine.begin() as connection:
            # RETURNING rather than a bare rowcount: a run that was validating
            # an upload has a second row to correct, and this is the only
            # moment its identity is known without a second query.
            interrupted = connection.execute(
                update(_RUNS)
                .where(
                    _RUNS.c.status == "running",
                    or_(_RUNS.c.heartbeat_at.is_(None), _RUNS.c.heartbeat_at < cutoff),
                )
                .values(
                    status="failed",
                    error_message=INTERRUPTED_MESSAGE,
                    finished_at=func.now(),
                )
                .returning(_RUNS.c.id, _RUNS.c.purpose)
            ).all()

        count = len(interrupted)
        if count:
            logger.warning(
                "Marked %d interrupted run(s) as failed (no heartbeat since %s): %s",
                count,
                cutoff.isoformat(timespec="seconds"),
                INTERRUPTED_MESSAGE,
            )
            _settle_interrupted_validations(engine, interrupted)
        return count
    finally:
        if owned:
            engine.dispose()


def _settle_interrupted_validations(
    engine: Engine, interrupted: Sequence[Row[tuple[uuid.UUID, str]]]
) -> None:
    """Park the uploads whose validation runs died with the last server.

    A strategy leaves ``validating`` only when its validation run reaches a
    verdict, and that verdict is normally written by the worker that finished
    the run. A worker killed mid-validation writes nothing, so failing the run
    here without failing the strategy leaves the upload permanently invisible:
    not in the catalogue (it is not ``active``), not runnable (it is not
    ``enabled``), and described to the student as something that will activate
    when it passes. Failing it is recoverable — the student re-uploads — and
    the run row keeps the reason.

    Imported inside the function because the worker module pulls in the engine,
    and with it pandas: the reconciler is small and is also imported by scripts
    and tests that have no use for either.
    """
    from src.workers.run_job import apply_validation_outcome

    for run_id, purpose in interrupted:
        if purpose != "validation":
            continue
        try:
            apply_validation_outcome(engine, run_id, "failed")
        except Exception:
            # Never take the boot down for a strategy row: the runs are already
            # correct, and the next restart re-runs nothing but this loop.
            logger.exception(
                "Could not park the strategy validated by interrupted run %s", run_id
            )


def orphaned_queued_run_ids(
    engine: Engine | None = None, limit: int = MAX_REQUEUED_RUNS
) -> list[uuid.UUID]:
    """Runs that were accepted but never claimed, oldest first.

    These are the rows the previous process had inserted and submitted (or was
    about to) when it went away. Nothing will ever claim them again on its own:
    submission happens once, in the request that created the row. Leaving them
    is worse than either alternative — the run sits in ``queued`` with no
    progress and no error, which is the one state the UI cannot explain.
    """
    owned = engine is None
    engine = engine or create_sync_engine()
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                select(_RUNS.c.id)
                .where(_RUNS.c.status == "queued")
                .order_by(_RUNS.c.created_at)
                .limit(limit)
            ).all()
        return [row[0] for row in rows]
    finally:
        if owned:
            engine.dispose()
