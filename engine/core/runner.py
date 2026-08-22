from logging import Logger
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo  # <-- ADDED for timezone fix

import pandas as pd

# VISUALIZER: tqdm is gone — progress is reported through the injected
# on_progress callback so it can reach a database row and then a browser.
from engine.analytics.reporting import generate_backtest_report
from engine.contracts.errors import NoMarketData, RunCancelled
from engine.core.executor import BacktestExecutor
from engine.core.utils import fetch_historical_data
from engine.strategies.portfolio_BASE.strategy import BasePortfolio

# Define the exchange timezone
NY_TZ = ZoneInfo("America/New_York")


class BacktestRunner:
    """
    Orchestrates the execution of a multi-ticker backtest using a unified
    portfolio model to ensure accuracy.
    """

    def __init__(
        self,
        portfolio: "BasePortfolio",
        start_date: str | datetime | pd.Timestamp,
        end_date: str | datetime | pd.Timestamp | None = None,
        initial_capital: float = 100000.0,
        slippage: float = 0.0,
        cost_model: Any = None,
        order_manager=None,
        on_progress: Callable[[int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        output_dir: str | None = None,
        strict: bool = False,
    ):
        """
        Initializes the BacktestRunner.
        """
        self.portfolio = portfolio
        self.logger = portfolio.logger
        self.total_start_capital = initial_capital
        self.order_manager = order_manager
        # VISUALIZER: task-4 seams. All four are per-instance, so a
        # worker process can run one of these at a time with no shared
        # state; the no-op defaults keep script use unchanged.
        self.on_progress: Callable[[int, str], None] = (
            on_progress if on_progress is not None else lambda pct, stage: None
        )
        self.should_cancel: Callable[[], bool] = (
            should_cancel if should_cancel is not None else lambda: False
        )
        self.output_dir: str | None = output_dir
        self.strict: bool = strict
        # VISUALIZER: the results frame used to be local to run(); the
        # caller needs it to build the equity curve.
        self.perf_df: pd.DataFrame | None = None

        # FIX 3: Use new timezone-aware method
        self.start_date: datetime = self._ensure_datetime(start_date)
        self.end_date: datetime = self._ensure_datetime(end_date, default_is_yesterday=True)

        # --- FIX 1: Save the *actual* backtest start date ---
        self.backtest_loop_start_date: datetime | None = self.start_date
        # --- END FIX 1 ---

        self.slippage: float = slippage
        self.cost_model: Any = cost_model

        lookback_days = getattr(self.portfolio, "lookback_days", 365)
        self.strategy_lookback_window = pd.Timedelta(days=lookback_days)
        self.logger.info(f"Using strategy lookback window of {lookback_days} days.")

        self.perf_records: list[dict[str, Any]] = []
        self.main_data_df: pd.DataFrame = pd.DataFrame()
        self.executor: BacktestExecutor | None = None

    def _ensure_datetime(self,
        dt_val: int | float | str | datetime,
        default_is_yesterday: bool = False
    ) -> datetime:
        """
        FIX 3: Converts input to a timezone-AWARE datetime object at midnight
        in 'America/New_York'.
        """
        try:
            pd_dt: datetime = pd.to_datetime(dt_val, errors="coerce")
            if pd.isna(pd_dt) and default_is_yesterday:
                ny_now = datetime.now(NY_TZ)
                yesterday: datetime = ny_now - timedelta(days=1)
                return yesterday
            elif pd.isna(pd_dt):
                return datetime.now(NY_TZ)

            # Create naive datetime at midnight, then localize to NY
            # Use replace() to correctly handle DST changes
            return datetime(pd_dt.year, pd_dt.month, pd_dt.day).replace(tzinfo=NY_TZ)
        except Exception as e:
            raise e


    def _prepare_data(self) -> bool:
        """
        Fetches, cleans, sorts, and prepares historical market data.
        (This function is now correct)
        """

        if not self.start_date or not self.end_date:
            self.logger.error("Invalid start or end date for data preparation.")
            return False

        # This is your correct "cold start" fix for the *data query*
        lookback_days = getattr(self.portfolio, "lookback_days", None)
        if lookback_days:
            adjusted_start = self.start_date - pd.Timedelta(days=lookback_days)
            # This 'self.start_date' is now only used for the *query*
            self.start_date = adjusted_start
            self.logger.info(
                f"Adjusted data query Start Date to {self.start_date} to include lookback_days={lookback_days}"
            )

        self.on_progress(0, "loading data")
        df = fetch_historical_data(self.portfolio, self.start_date, self.end_date)
        if df.empty:
            self.logger.error("No historical data found for the specified criteria.")
            # VISUALIZER: an empty frame used to abort quietly and report
            # a successful run with no results. Name the tickers and the
            # window instead so the failure is actionable in the UI.
            if self.strict:
                raise NoMarketData(
                    tickers=list(getattr(self.portfolio, "tickers", [])),
                    start=self.start_date,
                    end=self.end_date,
                )
            return False

        try:
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            self.main_data_df = df
            self.logger.info(f"Data prepared: {len(self.main_data_df)} rows loaded.")
            return True
        except Exception as e:
            self.logger.exception(f"Error during data preparation: {e}", exc_info=True)
            return False

    def _setup_executor(self) -> None:
        """Sets up the new unified BacktestExecutor."""
        self.executor = BacktestExecutor(
            initial_capital=self.total_start_capital,
            tickers=self.portfolio.tickers,
            slippage=self.slippage,
            cost_model=self.cost_model,
        )
        # Thread the OMS through the portfolio so it reaches StrategyContext
        # (the single shared seam, same as live); None keeps the direct path.
        self.portfolio.order_manager = self.order_manager
        self.portfolio._original_executor = getattr(self.portfolio, "executor", None)
        self.portfolio.executor = self.executor

    def _run_event_loop(self) -> None:
        """
        Runs the main backtest simulation loop, reporting progress and
        honouring cancellation once per timestamp group.
        """
        if self.main_data_df.empty or self.executor is None:
            self.logger.error("Cannot run event loop: Data or executor not ready.")
            return

        self.logger.info("Starting backtest event loop...")

        poll_td = pd.Timedelta(seconds=self.portfolio.poll_interval)

        # This series is built from the *full* dataframe, so lookups are correct
        timestamps_series = self.main_data_df["timestamp"]
        self.perf_records = []
        last_poll_time: pd.Timestamp | None = None

        # --- FIX 2: Filter the timestamps we iterate over ---

        # 1. Group the *full* dataframe once for efficient lookups
        data_groups = self.main_data_df.groupby("timestamp", sort=True)

        # 2. Get all unique timestamps, which are already sorted
        all_timestamps = self.main_data_df["timestamp"].unique()

        # 3. Filter them to start *only* from the intended backtest start date
        loop_timestamps = all_timestamps[
            all_timestamps >= self.backtest_loop_start_date
        ]
        if len(loop_timestamps) == 0:
            self.logger.error(
                f"No data found on or after the intended start date: {self.backtest_loop_start_date}"
            )
            return
        # --- END FIX 2 ---

        # VISUALIZER: tqdm replaced by the injected progress callback.
        # Percentage is the position in the filtered timestamp index, so
        # it is monotonic and ends at 100 exactly like the old bar did.
        total_steps = len(loop_timestamps)
        last_reported_pct = -1

        for step, current_timestamp in enumerate(loop_timestamps, start=1):
            # VISUALIZER: cancellation is checked once per timestamp
            # group — the finest granularity that costs nothing, since
            # the callback itself is throttled by the caller.
            if self.should_cancel():
                raise RunCancelled(
                    f"Run cancelled at {current_timestamp} ({step}/{total_steps} steps)"
                )

            pct = int(step * 100 / total_steps)
            if pct != last_reported_pct:
                last_reported_pct = pct
                self.on_progress(pct, "simulating")

            # Get the data chunk for this timestamp from the *full* group
            try:
                current_data_chunk = data_groups.get_group(current_timestamp)
            except KeyError:
                continue  # Should not happen, but safe to check

            price_updates = dict(
                zip(current_data_chunk["ticker"], current_data_chunk["close_price"])
            )
            for ticker, price in price_updates.items():
                if pd.notna(price):
                    self.executor.update_price(ticker, float(price))

            # Pump the OMS at EVERY bar, before the strategy poll gate, so
            # sliced schedules (TWAP/VWAP children) fill at intermediate bars
            # instead of bunching at the next poll — this is the backtest
            # counterpart of the live engine's OMS tick thread. Prices were
            # just updated above, and strategy calls / perf records stay
            # behind the poll gate, so non-OMS runs are unaffected.
            if self.order_manager is not None:
                try:
                    pump_time = current_timestamp.to_pydatetime()
                    self.order_manager.manage_order(
                        now=pump_time,
                        execute_child=lambda child, _ts=pump_time: (
                            self.executor.execute_child_order(child, timestamp=_ts)
                        ),
                    )
                except Exception as e:
                    self.logger.exception(
                        f"OMS pump failed at {current_timestamp}: {e}", exc_info=True
                    )

            if last_poll_time and (current_timestamp - last_poll_time) < poll_td:
                continue
            last_poll_time = current_timestamp

            # This logic now works perfectly:
            # current_timestamp is (e.g.) `2025-01-02 04:30:00`
            # window_start_time is `2024-10-04 04:30:00` (i.e., 90 days ago)
            window_start_time = current_timestamp - self.strategy_lookback_window

            # start_index will search the *full* timestamps_series and find the correct index in 2024
            start_index = timestamps_series.searchsorted(window_start_time, side="left")

            # end_index is the absolute index of the current bar
            end_index = current_data_chunk.index.max()

            if start_index < 0:
                start_index = 0

            # This slice is now correct: [data_from_90_days_ago ... data_from_today]
            historical_slice_df = self.main_data_df.iloc[start_index : end_index + 1]

            if not historical_slice_df.empty:
                try:
                    sim_time = current_timestamp.to_pydatetime()
                    data_dict = self.executor.get_data_feeds()
                    data_dict["MARKET_DATA"] = historical_slice_df
                    self.portfolio.generate_signals_and_trade(
                        data_dict, current_time=sim_time
                    )
                except RunCancelled:
                    # VISUALIZER: never swallow a cancellation raised
                    # from inside strategy code.
                    raise
                except Exception as e:
                    self.logger.exception(
                        f"Error in strategy at {current_timestamp}: {e}", exc_info=True
                    )
                    # VISUALIZER: swallowing this is right for the batch CLI —
                    # one broken portfolio should not stop the other eight —
                    # but wrong for a single run whose whole purpose is to
                    # certify that a strategy works. Without the re-raise an
                    # upload that throws on every bar finishes as a completed
                    # run with zero fills, and the validation gate activates it.
                    if self.strict:
                        raise

            record = {"timestamp": current_timestamp}
            for ticker in self.portfolio.tickers:
                record[ticker] = self.executor.get_position_value(ticker)
            record["portfolio_value"] = self.executor.get_port_notional()
            self.perf_records.append(record)

        self.logger.info("Event loop finished.")

    def _calculate_results(self) -> pd.DataFrame | None:
        """Calculates performance metrics from recorded data."""
        if not self.perf_records:
            self.logger.warning("No performance records generated during the backtest.")
            return None
        try:
            perf_df = pd.DataFrame(self.perf_records)
            perf_df["timestamp"] = pd.to_datetime(perf_df["timestamp"], utc=True, errors="coerce")
            perf_df.sort_values("timestamp", inplace=True)
            perf_df.reset_index(drop=True, inplace=True)

            value_cols = list(self.portfolio.tickers) + ["portfolio_value"]
            for col in value_cols:
                if col in perf_df.columns:
                    perf_df[col] = pd.to_numeric(perf_df[col], errors="coerce")

            if self.total_start_capital > 0:
                perf_df["pnl_pct"] = (
                    perf_df["portfolio_value"] - self.total_start_capital
                ) / self.total_start_capital
            else:
                perf_df["pnl_pct"] = 0.0

            self.logger.info("Performance DataFrame calculated.")
            return perf_df
        except Exception as e:
            self.logger.exception(
                f"Error calculating results DataFrame: {e}", exc_info=True
            )
            return None

    def _restore_executor(self) -> None:
        """Restores the portfolio's original executor."""
        if hasattr(self.portfolio, "_original_executor"):
            self.portfolio.executor = self.portfolio._original_executor
            try:
                del self.portfolio._original_executor
            except AttributeError:
                pass
            self.logger.info("Restored original portfolio executor.")
        else:
            if isinstance(getattr(self.portfolio, "executor", None), BacktestExecutor):
                self.logger.warning(
                    "Could not restore original executor: '_original_executor' attribute missing."
                )
            elif getattr(self.portfolio, "executor", None) is None:
                self.logger.info("Portfolio executor was None or already restored.")

    def run(self) -> list[str] | None:
        """Executes the entire backtest process."""
        self.logger.info("===== Starting Backtest Run =====")
        # VISUALIZER: second cancellation gate, before the data load.
        # Fetching a multi-year window is the other multi-minute step
        # worth skipping for an already-cancelled run.
        if self.should_cancel():
            raise RunCancelled("Run cancelled before data preparation")
        if not hasattr(self.portfolio, "portfolio_id") or not hasattr(
            self.portfolio, "tickers"
        ):
            self.logger.error(
                "Portfolio object is missing required attributes. Aborting."
            )
            return

        self.logger.info(f"Portfolio ID: {self.portfolio.portfolio_id}")
        self.logger.info(f"Tickers: {self.portfolio.tickers}")

        perf_df = None

        try:
            if not self._prepare_data():
                self.logger.error("Backtest aborted due to data preparation failure.")
                return

            self._setup_executor()
            self._run_event_loop()
            perf_df: pd.DataFrame = self._calculate_results()
            # VISUALIZER: keep the frame reachable for run_single.
            self.perf_df = perf_df

            if perf_df is not None and not perf_df.empty:
                self.on_progress(100, "writing report")
                generate_backtest_report(
                    portfolio=self.portfolio,
                    perf_df=perf_df,
                    initial_capital=self.total_start_capital,
                    full_historical_data=self.main_data_df,
                    # VISUALIZER: artifacts land in this run's own
                    # directory instead of a path relative to the cwd.
                    out_dir=self.output_dir,
                )
                if self.executor:
                    trade_log = self.executor.dump_trade_log()
                else:
                    trade_log = None
                return trade_log
            else:
                self.logger.warning(
                    "Skipping report generation due to empty or invalid results."
                )
        except RunCancelled:
            # VISUALIZER: see backtest_engine.run — cancellations skip
            # the traceback and are never swallowed.
            self.logger.info("Backtest run cancelled by request.")
            raise
        except Exception as e:
            self.logger.exception(
                f"An critical error occurred during the backtest run: {e}",
                exc_info=True,
            )
            # VISUALIZER: in strict mode the caller owns the failure.
            if self.strict:
                raise
        finally:
            self._restore_executor()
