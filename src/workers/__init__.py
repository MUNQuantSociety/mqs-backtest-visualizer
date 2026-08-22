"""Job execution: the processes that actually run backtests.

Deliberately empty of imports. On Windows a ``ProcessPoolExecutor`` *spawns*
its workers, which re-imports every module on the path to the submitted
callable — so this package must cost nothing to import and must have no
side effects. Import the module you need
(``src.workers.job_manager``, ``src.workers.run_job``,
``src.workers.reconciler``) directly.
"""
