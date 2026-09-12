"""Transient job state. Only successful reports are written to PostgreSQL.

A single API process owns the queue; unfinished jobs do not survive restart.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import case, exists, select, update
from src.core.config import settings
from src.db.engine import create_sync_engine
from src.models import BacktestReport, Strategy
from src.repositories import reports
from src.schemas.backtests import BacktestDetail
from src.workers.report_job import RunSpec, execute_report

logger = logging.getLogger(__name__)
TERMINAL_TTL_SECONDS = 3600

@dataclass
class _Job:
    spec: RunSpec
    state: object
    lock: object = field(default_factory=threading.RLock)
    future: object = None
    finished_at: float | None = None
    detail: BacktestDetail | None = None
    saved: bool = False

class JobManager:
    def __init__(self, max_workers=None):
        self._max_workers = max(int(max_workers or settings.max_concurrent_runs), 1)
        self._pool = self._ipc = None
        self._jobs = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def max_workers(self):
        return self._max_workers

    @property
    def running(self):
        return self._pool is not None and not self._closed

    def start(self):
        if self.running:
            return
        context = multiprocessing.get_context("spawn")
        self._ipc = context.Manager()
        self._pool = ProcessPoolExecutor(max_workers=self._max_workers, mp_context=context)
        self._closed = False
        logger.info("Job manager started with %d transient workers", self._max_workers)

    def _prune(self):
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at > TERMINAL_TTL_SECONDS:
                del self._jobs[key]

    def _lookup(self, key):
        with self._lock:
            self._prune()
            return self._jobs.get(str(key))

    def register(self, spec):
        if spec.purpose == "user" and spec.owner_id is None:
            raise ValueError("A user backtest requires an owner.")
        with self._lock:
            if not self.running:
                raise RuntimeError("The job manager is not running.")
            self._prune()
            key = str(spec.id)
            if key in self._jobs:
                raise ValueError("This job ID is already registered.")
            self._jobs[key] = _Job(spec, self._ipc.dict(
                status="queued", progress_pct=0, cancel_requested=False, error_message=None))

    def submit(self, run_id):
        job = self._lookup(run_id)
        if job is None or not self.running:
            raise RuntimeError("The job is unknown or its API process has restarted.")
        with job.lock:
            if job.future is not None:
                return job.future
            if job.finished_at is not None:
                raise RuntimeError("This job has already finished.")
            try:
                job.future = self._pool.submit(execute_report, job.spec, job.state)
            except Exception as exc:
                self._fail(job, f"Could not queue the backtest: {exc}")
                self._settle_validation(job, False)
                raise RuntimeError(str(exc)) from exc
            job.future.add_done_callback(lambda future: self._on_done(job, future))
            return job.future

    def get_detail(self, run_id, owner_id):
        job = self._lookup(run_id)
        if job is None or job.spec.owner_id != owner_id:
            return None
        with job.lock:
            if job.saved:
                return None
            if job.detail is not None:
                return job.detail.model_copy(deep=True)
            state = dict(job.state)
            return job.spec.empty_detail(state["status"], state["progress_pct"], state.get("error_message"))

    def cancel(self, run_id, owner_id):
        job = self._lookup(run_id)
        if job is None or job.spec.owner_id != owner_id:
            return "not_found"
        with job.lock:
            if job.saved:
                return "saved"
            if job.finished_at is not None:
                with self._lock:
                    self._jobs.pop(str(run_id), None)
                return "deleted"
            job.state["cancel_requested"] = True
            if job.future is None:
                self._fail(job, "Cancelled by user")
            else:
                job.future.cancel()
            return "cancel_requested"

    def cancel_internal(self, run_id):
        job = self._lookup(run_id)
        return self.cancel(run_id, job.spec.owner_id) if job else "not_found"

    def submitted_run_ids(self):
        with self._lock:
            return [key for key, job in self._jobs.items() if job.finished_at is None]

    def _fail(self, job, message):
        message = str(message)[:2000]
        job.detail = job.spec.empty_detail("failed", 0, message)
        job.state.update(status="failed", error_message=message)
        job.finished_at = time.monotonic()
        logger.warning("Run %s failed; no report saved: %s", job.spec.id, message)

    def _on_done(self, job, future):
        # Cancel and final save use this same lock; callers enter from threads,
        # never from the event loop, so a DB write cannot freeze HTTP handling.
        with job.lock:
            try:
                if self._closed or job.state.get("cancel_requested") or future.cancelled():
                    self._fail(job, "Interrupted by server shutdown" if self._closed else "Cancelled by user")
                    self._settle_validation(job, False)
                    return
                outcome = future.result()
                if "error" in outcome:
                    self._fail(job, outcome["error"])
                    self._settle_validation(job, False)
                    return
                detail = BacktestDetail.model_validate(outcome["report"])
                if job.spec.owner_id is not None:
                    engine = create_sync_engine()
                    try:
                        reports.save(engine, job.spec.owner_id, detail)
                    finally:
                        engine.dispose()
                    job.saved = True
                else:
                    # Internal validation without a user has no personal report.
                    job.detail = detail
                job.state.update(status="completed", progress_pct=100)
                job.finished_at = time.monotonic()
                self._settle_validation(job, True)
                logger.info("COMPLETED | run=%s report_saved=%s", job.spec.id, job.saved)
            except Exception as exc:
                if not job.saved:
                    self._fail(job, f"Backtest or report persistence failed: {type(exc).__name__}: {exc}")
                    self._settle_validation(job, False)
                else:
                    logger.exception("Report %s saved, but final notification failed", job.spec.id)

    @staticmethod
    def _settle_validation(job, passed):
        if job.spec.purpose != "validation":
            return
        engine = create_sync_engine()
        try:
            with engine.begin() as connection:
                connection.execute(update(Strategy).where(
                    Strategy.key == job.spec.strategy_key, Strategy.kind == "user",
                ).values(status="active" if passed else "failed_validation", enabled=passed,
                         validation_job_id=job.spec.id))
        except Exception:
            logger.exception("Could not record validation outcome for %s", job.spec.strategy_key)
        finally:
            engine.dispose()

    def shutdown(self, wait=False):
        self._closed = True
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if job.finished_at is None:
                    job.state["cancel_requested"] = True
                    if job.future is None:
                        self._fail(job, "Interrupted by server shutdown")
                        self._settle_validation(job, False)
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=True)
        # IPC must outlive running callbacks, including their final state write.
        if wait:
            self._close_ipc()
        else:
            threading.Thread(target=self._close_when_finished, args=(jobs,), daemon=True).start()

    def _close_when_finished(self, jobs):
        while any(job.finished_at is None for job in jobs):
            time.sleep(0.1)
        self._close_ipc()

    def _close_ipc(self):
        with self._lock:
            ipc, self._ipc = self._ipc, None
        if ipc is not None:
            ipc.shutdown()

_manager = None

def get_job_manager():
    if _manager is None:
        raise RuntimeError("The job manager has not been started.")
    return _manager

def start_job_manager(max_workers=None):
    global _manager
    if _manager is None:
        _manager = JobManager(max_workers)
    _manager.start()
    return _manager

def stop_job_manager(wait=False):
    global _manager
    manager, _manager = _manager, None
    if manager is not None:
        manager.shutdown(wait=wait)

@asynccontextmanager
async def job_manager_lifespan(_app=None) -> AsyncIterator[None]:
    try:
        await asyncio.to_thread(_recover_validation_outcomes)
    except Exception:
        logger.exception("Could not recover interrupted strategy validation metadata")
    start_job_manager()
    try:
        yield
    finally:
        stop_job_manager()


def _recover_validation_outcomes():
    """Recover strategy metadata only; never create or restart a run/report."""
    engine = create_sync_engine()
    try:
        saved = exists(select(BacktestReport.id).where(BacktestReport.id == Strategy.validation_job_id))
        with engine.begin() as connection:
            connection.execute(update(Strategy).where(
                Strategy.kind == "user", Strategy.status == "validating",
                Strategy.validation_job_id.is_not(None),
            ).values(status=case((saved, "active"), else_="failed_validation"), enabled=saved))
    finally:
        engine.dispose()

@asynccontextmanager
async def application_lifespan(app=None) -> AsyncIterator[None]:
    from src.db.init import database_lifespan
    from src.core.logging_config import configure_logging
    configure_logging(settings.log_level, non_blocking=True)
    async with database_lifespan(app):
        async with job_manager_lifespan(app):
            logger.info("READY | Completed-report storage and transient workers ready")
            yield
