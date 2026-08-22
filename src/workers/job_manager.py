"""The process pool that runs backtests, and its lifespan.

An event-mode backtest is minutes of single-core, GIL-holding Python. Running
it inline would freeze the API; running it in a thread would starve the event
loop just as effectively. A ``ProcessPoolExecutor`` with a couple of workers
gives queueing and a responsive API with no extra infrastructure — the price is
that the run and the request that submitted it share nothing but a run id,
which is why :mod:`src.workers.run_job` reads everything it needs from the
database.

**The pool is created in the lifespan and nowhere else.** On Windows a pool
spawns its workers, and spawning re-imports the module tree; a pool built at
import time would therefore be built again inside every worker it creates, and
under ``uvicorn --reload`` that recurses into a fork bomb the first time a file
changes. The lifespan runs exactly once per real server process, which is the
only place it is safe.

Shutdown does not wait for running backtests. Blocking a deploy or a Ctrl-C for
the ten minutes a long run might have left would be worse than losing it, and
losing it is recoverable: the reconciler marks abandoned rows ``failed`` on the
next boot, with a message saying exactly that.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import asynccontextmanager

from src.core.config import settings
from src.workers.reconciler import orphaned_queued_run_ids, reconcile_interrupted_runs
from src.workers.run_job import fail_running_run, run_job

logger = logging.getLogger(__name__)


class JobManager:
    """Owns the worker pool and the futures currently in it.

    One instance per API process, created by the lifespan. Everything about it
    is deliberately small: the pool is the queue, the run row is the state, and
    this class is only the handle that submits and shuts down.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        self._max_workers = max(int(max_workers or settings.max_concurrent_runs), 1)
        self._pool: ProcessPoolExecutor | None = None
        self._futures: dict[str, Future] = {}

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def running(self) -> bool:
        return self._pool is not None

    def start(self) -> None:
        """Create the pool. Idempotent, so a double-started lifespan is fine."""
        if self._pool is not None:
            return
        self._pool = ProcessPoolExecutor(
            max_workers=self._max_workers,
            # Spawn explicitly rather than inheriting the platform default.
            # Under fork (the Linux default) a worker would inherit the API's
            # asyncpg pool and event loop — two processes reading the same
            # sockets, which fails as data corruption rather than as an error.
            # Spawn is also what Windows does, so behaviour matches everywhere.
            mp_context=multiprocessing.get_context("spawn"),
        )
        logger.info("Job manager started with %d worker process(es)", self._max_workers)

    def submit(self, run_id: uuid.UUID | str) -> Future:
        """Queue a run for execution and return its future.

        Raises ``RuntimeError`` if the pool is not running or has broken. The
        caller is expected to catch that and mark the run ``failed`` — a run
        row that exists but was never dispatched is the one failure mode the
        student cannot see, because it looks exactly like a busy queue.
        """
        if self._pool is None:
            raise RuntimeError(
                "the job manager is not running; it is started by the "
                "application lifespan"
            )

        key = str(run_id)
        try:
            future = self._pool.submit(run_job, key)
        except Exception as exc:  # pool shut down, or broken by a dead worker
            raise RuntimeError(f"could not queue run {key}: {exc}") from exc

        self._futures[key] = future
        future.add_done_callback(lambda done, key=key: self._on_done(key, done))
        logger.info("Run %s queued (%d in flight)", key, len(self._futures))
        return future

    def submitted_run_ids(self) -> list[str]:
        """Runs this process has submitted and not yet seen finish."""
        return [key for key, future in self._futures.items() if not future.done()]

    def shutdown(self, wait: bool = False) -> None:
        """Stop the pool. Does not wait for running backtests — see module doc."""
        pool, self._pool = self._pool, None
        self._futures.clear()
        if pool is None:
            return
        pool.shutdown(wait=wait, cancel_futures=True)
        logger.info("Job manager stopped")

    def _on_done(self, key: str, future: Future) -> None:
        """Close the books on a job, including the ways it can end silently.

        Nothing awaits these futures, so without this callback a worker that
        died would leave its exception sitting inside one, unread. ``run_job``
        marks its own failures, but it cannot mark the failure that kills it:
        an OOM kill, a segfault in a native library, a pool broken by an
        earlier death. Those end here, as an exception on the future and a run
        row still saying ``running`` — which the frontend polls forever.

        Blocking on a database write is acceptable here because this runs on
        the pool's own callback thread, not on the event loop, and only on the
        path where a worker has already died.
        """
        self._futures.pop(key, None)
        if future.cancelled():
            # Cancelled before it was claimed, so the row is still ``queued``
            # and the next boot's reconciler hands it back to a pool.
            logger.info("Run %s was cancelled before it started", key)
            return
        error = future.exception()
        if error is not None:
            logger.error("Run %s worker raised: %r", key, error)
            self._fail_dead_run(key, error)
            return
        logger.info("Run %s worker finished: %s", key, future.result())

    @staticmethod
    def _fail_dead_run(key: str, error: BaseException) -> None:
        """Give the run of a dead worker a terminal state and a reason."""
        message = f"The worker process died: {type(error).__name__}: {error}"
        try:
            if fail_running_run(key, message):
                logger.warning("Run %s marked failed after its worker died", key)
        except Exception:
            # Last resort only: the startup reconciler picks up whatever is
            # still ``running`` the next time the server boots.
            logger.exception("Run %s could not be marked failed after its worker died", key)


_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """The process-wide job manager.

    Raises rather than creating one on demand: a manager built outside the
    lifespan is a pool built at an unpredictable moment, which is the failure
    this whole module is arranged to prevent.
    """
    if _manager is None:
        raise RuntimeError(
            "the job manager has not been started; the application lifespan "
            "(src.workers.job_manager.job_manager_lifespan) does that"
        )
    return _manager


def start_job_manager(max_workers: int | None = None) -> JobManager:
    """Create and start the singleton. Returns the existing one if started."""
    global _manager
    if _manager is None:
        _manager = JobManager(max_workers=max_workers)
    _manager.start()
    return _manager


def stop_job_manager(wait: bool = False) -> None:
    """Shut the singleton down and forget it."""
    global _manager
    if _manager is not None:
        _manager.shutdown(wait=wait)
    _manager = None


@asynccontextmanager
async def job_manager_lifespan(_app: object = None) -> AsyncIterator[None]:
    """FastAPI lifespan: reconcile, start the pool, requeue, stop on the way out.

    The order matters. Reconciliation must finish before the pool accepts
    anything, or it would mark the runs it is about to start as interrupted.

    A database that is unreachable at boot is logged, not raised: ``/live/*``
    needs no database at all, and refusing to start the app would take those
    routes down too. Submission of new runs still works — the worker connects
    on its own — and stranded rows are reconciled at the next boot.
    """
    try:
        await asyncio.to_thread(reconcile_interrupted_runs)
    except Exception as exc:
        logger.warning(
            "Could not reconcile interrupted runs at startup (%s: %s); "
            "any run still marked running will be corrected on the next boot.",
            type(exc).__name__,
            exc,
        )

    manager = start_job_manager()
    try:
        await asyncio.to_thread(_requeue_orphans, manager)
    except Exception as exc:
        logger.warning(
            "Could not requeue orphaned runs at startup (%s: %s)",
            type(exc).__name__,
            exc,
        )

    try:
        yield
    finally:
        stop_job_manager()


def _requeue_orphans(manager: JobManager) -> None:
    """Resubmit runs the previous process accepted but never got to start."""
    for run_id in orphaned_queued_run_ids():
        try:
            manager.submit(run_id)
        except RuntimeError as exc:
            logger.warning("Could not requeue orphaned run %s: %s", run_id, exc)
            return


@asynccontextmanager
async def application_lifespan(app: object = None) -> AsyncIterator[None]:
    """Everything the server needs at boot: schema, then workers.

    ``server.py`` should use this in place of ``database_lifespan`` — the
    schema has to exist before the reconciler updates rows in it, and the pool
    has to stop before the app's own shutdown completes.
    """
    from src.db.init import database_lifespan

    async with database_lifespan(app):
        async with job_manager_lifespan(app):
            yield
