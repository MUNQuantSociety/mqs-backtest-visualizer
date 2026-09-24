# engine/strategies/portfolio_BASE/strategy.py  (vendored from MQSMaster)

import copy
import importlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from engine.indicators.base import Indicator
from engine.data.fmp import FMPDataAdapter, fetch_daily_history, market_data_source
from engine.data.bar_interval import (
    DAILY_BAR_SECONDS,
    bar_minutes,
    is_intraday,
    warmup_calendar_days,
)
from engine.data.intraday import fetch_intraday_bars
from engine.strategies.order_interface import StrategyContext


_NY_TZ = ZoneInfo("America/New_York")


def _camel_to_snake(name: str) -> str:
    """Converts a CamelCase string to snake_case for dynamic module loading."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class BasePortfolio(ABC):
    """
    Base class for all portfolio strategies, featuring a dynamic, stateful
    indicator manager and the StrategyContext API.

    A strategy declares what it needs with the three class attributes below and
    implements :meth:`OnData`. Nothing else is required: this ``__init__`` reads
    the declarations and does the wiring, so a strategy file holds trading logic
    and nothing about how the framework is plumbed together. Writing an explicit
    ``__init__`` that calls ``super().__init__(...)`` and then
    ``RegisterIndicatorSet``/``AddIndicator`` by hand still works exactly as
    before — the declarative path is an addition, not a replacement, and every
    strategy already uploaded keeps running unchanged.
    """

    # Indicators to build before the first bar, as
    # ``"attribute_name": ("IndicatorClassName", {kwargs})`` — one instance per
    # ticker in the universe, reachable as ``self.attribute_name[ticker]``.
    # A third element names a single ticker instead, and then the attribute is
    # the indicator itself: ``"vix_ema": ("ExponentialMovingAverage",
    # {"period": 10}, "^VIX")`` -> ``self.vix_ema.Current``.
    INDICATORS: dict[str, tuple] = {}

    # Instance state, as ``"attribute_name": default``. Each default is deep
    # copied per instance, so a mutable one ({} or []) is never shared between
    # two strategies the way a bare class attribute would be.
    STATE: dict[str, Any] = {}

    # Instance state that is per ticker, as ``"attribute_name": default``.
    # Each becomes ``{ticker: deepcopy(default)}`` over the whole universe.
    PER_TICKER_STATE: dict[str, Any] = {}

    def __init__(
        self,
        db_connector,
        executor,
        debug=False,
        config_dict=None,
        backtest_start_date: Optional[datetime] = None,
        order_manager=None,
    ):
        """
        Initializes the base portfolio, loading configuration.
        """
        self.db = db_connector
        self.executor = executor
        self.order_manager = order_manager
        self.running = True
        self.debug = debug
        self.backtest_start_date = backtest_start_date
        self._last_processed_timestamp: datetime | None = None

        # The declared default is None, so a caller that omits the config gets
        # the documented defaults below instead of an AttributeError on .get.
        config_dict = config_dict or {}

        # Every key config.json can carry is assigned here. The whole document
        # is kept as well, because PortfolioConfig allows extra keys: a
        # strategy-specific block has nowhere else to be read from.
        self.config: dict[str, Any] = dict(config_dict)
        self.portfolio_id: str = config_dict.get("PORTFOLIO_ID", "0")
        self.tickers: list[str] = config_dict.get("TICKERS", [])
        self.poll_interval: int = config_dict.get("INTERVAL", 60)
        self.lookback_days: int = config_dict.get("LOOKBACK_DAYS", 30)
        # VISUALIZER: the bar size the engine loads. Daily unless a run asks
        # for intraday bars; an unsupported size fails here, before any fetch.
        self.bar_interval_seconds: int = int(
            config_dict.get("BAR_INTERVAL_SECONDS", DAILY_BAR_SECONDS)
        )
        bar_minutes(self.bar_interval_seconds)
        self.portfolio_weights: list[Any] | None = config_dict.get("WEIGHTS")
        self.data_feeds: list[str] = config_dict.get(
            "DATA_FEEDS",
            ["MARKET_DATA", "POSITIONS", "CASH_EQUITY", "PORT_NOTIONAL"]
        )
        # Listing venue for the universe. Carried for strategies and reporting
        # that ask; the vendored engine's data path is venue-agnostic.
        self.exchange: str | None = config_dict.get("EXCH")
        # The OMS block is read so it stops being a config key that silently
        # goes nowhere, but it is INERT in this application: the OMS belongs to
        # the trading system and is not vendored, so BacktestEngine always
        # passes order_manager=None (see backtest_engine.py, "upstream built a
        # per-portfolio OMS here"). Present for inspection, not for execution.
        self.oms_config: dict[str, Any] | None = config_dict.get("OMS")
        # Overridable per portfolio, as DEFAULT_INITIAL_CAPITAL claims to be.
        self.initial_capital: float = float(
            config_dict.get("INITIAL_CAPITAL", self.DEFAULT_INITIAL_CAPITAL)
        )

        self.logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__name__}_{self.portfolio_id}",
        )
        self.logger.info(
            "Initialized portfolio %s with %s tickers.",
            self.portfolio_id,
            len(self.tickers)
        )

        # What StrategyContext sees. The lowercase keys are the ones
        # order_interface reads by name; "exchange", "oms" and the raw "config"
        # are carried so a strategy reaching the context is not cut off from
        # config keys its own instance can already see.
        self.portfolio_config_dict: dict[str, Any] = {
            "id": self.portfolio_id,
            "tickers": self.tickers,
            "weights": self.portfolio_weights,
            "poll_interval": self.poll_interval,
            "lookback_days": self.lookback_days,
            "bar_interval_seconds": self.bar_interval_seconds,
            "exchange": self.exchange,
            "oms": self.oms_config,
            "config": self.config,
        }

        # --- Indicator Management ---
        self._indicators: list[Indicator] = []

        # --- Declared surface ---
        # State first: a strategy's OnData may touch it on the very first bar,
        # and building an indicator is the expensive step that can fail.
        self._init_declared_state()
        self._register_declared_indicators()

    def _init_declared_state(self) -> None:
        """Assign STATE and PER_TICKER_STATE onto this instance.

        Deep copied rather than referenced: two runs of the same strategy in one
        process would otherwise accumulate into the same dict on the class.
        """
        for attr_name, default in self.STATE.items():
            setattr(self, attr_name, copy.deepcopy(default))

        for attr_name, default in self.PER_TICKER_STATE.items():
            setattr(
                self,
                attr_name,
                {ticker: copy.deepcopy(default) for ticker in self.tickers},
            )

    def _count_diagnostic(self, ticker_diagnostics: dict[str, Any], key: str) -> None:
        """Increment one counter on both the portfolio and one ticker.

        ``run_single`` reads whatever ``strategy_diagnostics`` a strategy leaves
        behind and puts it in the report, so which counters exist is the
        strategy's business; keeping the two levels in step is the framework's.
        """
        self.strategy_diagnostics[key] += 1
        ticker_diagnostics[key] += 1

    def _register_declared_indicators(self) -> None:
        """Build every indicator named in INDICATORS, in declaration order.

        Order is the class's, because a strategy that declares a whole-universe
        set and then one single-ticker indicator warms them in that sequence.
        """
        for attr_name, definition in self.INDICATORS.items():
            class_name, kwargs, *single_ticker = definition
            if single_ticker:
                setattr(
                    self,
                    attr_name,
                    self.AddIndicator(class_name, single_ticker[0], **kwargs),
                )
            else:
                self.RegisterIndicatorSet({attr_name: (class_name, kwargs)})

    def _build_indicator_update_payload(self,
        indicator: Indicator,
        row
    ) -> tuple[Any | Literal['close_price'], float, dict[Any, Any]] | None:
        """
        Build a consistent Update() payload for an indicator from a market-data row.
        """
        price_col = (
            getattr(indicator, "price_col", None)
            or getattr(indicator, "close_col", None)
            or "close_price"
        )

        if not hasattr(row, price_col):
            return None

        price_value = getattr(row, price_col)
        if pd.isna(price_value):
            return None

        update_kwargs = {}

        vol_col = getattr(indicator, "vol_col", None)
        if vol_col and hasattr(row, vol_col):
            vol_value = getattr(row, vol_col)
            if pd.notna(vol_value):
                update_kwargs[vol_col] = float(vol_value)
                # Keep a normalized alias for indicators that read `volume` directly.
                update_kwargs["volume"] = float(vol_value)

        for indicator_attr in ("high_col", "low_col", "close_col"):
            col_name = getattr(indicator, indicator_attr, None)
            if col_name and hasattr(row, col_name):
                col_value = getattr(row, col_name)
                if pd.notna(col_value):
                    update_kwargs[col_name] = float(col_value)

        return price_col, float(price_value), update_kwargs

    def _update_indicator_from_row(self, indicator: Indicator, row) -> bool:
        """Update one indicator from one row; returns True if an update was applied."""
        payload: Any | None = self._build_indicator_update_payload(indicator, row)
        if payload is None:
            return False

        _, price_value, update_kwargs = payload
        try:
            if update_kwargs:
                indicator.Update(row.timestamp, price_value, **update_kwargs)
            else:
                indicator.Update(row.timestamp, price_value)
        except TypeError:
            # Some indicators accept only (timestamp, data_point).
            indicator.Update(row.timestamp, price_value)
        return True

    # --- DYNAMIC INDICATOR FACTORY ---
    def AddIndicator(
        self,
        indicator_class_name: str,
        ticker: str,
        **kwargs
    ) -> Indicator:
        """
        Dynamically loads, instantiates, warms up, and registers an indicator.
        This is the scalable factory for all indicators.

        Args:
            indicator_class_name (str): The CamelCase name of the indicator class (e.g., "SimpleMovingAverage").
            ticker (str): The ticker the indicator should run on.
            **kwargs: Keyword arguments for the indicator (e.g., period=50).

        Returns:
            An instance of the requested indicator, fully warmed-up and ready to use.
        """
        if ticker not in self.tickers:
            raise ValueError(
                f"Ticker '{ticker}' is not part of this portfolio's universe."
            )

        # VISUALIZER: one import path only. Upstream tried
        # "src.portfolios.indicators.*" then fell back to
        # "portfolios.indicators.*" depending on how the trading system was
        # launched; the vendored engine has exactly one package root, so a
        # miss here is a real error worth raising. The snake_case-file ->
        # CamelCase-class convention is unchanged
        # (relative_strength_index.py -> RelativeStrengthIndex).
        module_name = _camel_to_snake(indicator_class_name)
        try:
            module = importlib.import_module(f"engine.indicators.{module_name}")
            indicator_class = getattr(module, indicator_class_name)
        except (ImportError, AttributeError) as e:
            self.logger.error(
                "Could not dynamically load indicator '%s' from "
                "engine.indicators.%s.\nDetails: %s",
                indicator_class_name,
                module_name,
                e,
            )
            raise

        indicator = indicator_class(ticker=ticker, **kwargs)

        warmup_days = int(kwargs.get("period", 20)) * 1.7
        end_time = self.backtest_start_date or datetime.now()
        start_time = end_time - timedelta(days=warmup_days)

        if is_intraday(self.bar_interval_seconds):
            # VISUALIZER: warm with bars of the run's own size, so an
            # indicator's period counts the same bars before and during the
            # simulation. Completed days only, as on the daily path.
            minutes = bar_minutes(self.bar_interval_seconds)
            warmup_start = end_time - timedelta(
                days=warmup_calendar_days(int(kwargs.get("period", 20)), minutes)
            )
            frame = fetch_intraday_bars(
                self.db, [ticker], warmup_start, end_time - timedelta(days=1), minutes,
                require_all=False,
            )
            result = {"status": "success", "data": frame.to_dict("records")}
        elif market_data_source() == "fmp":
            # Warm indicators only with completed days before the simulation.
            # A newly listed ticker may have no warmup yet; it becomes ready
            # naturally as the event loop receives bars.
            fetch = self.db.get_daily_history if isinstance(self.db, FMPDataAdapter) else fetch_daily_history
            frame = fetch(
                [ticker], start_time.date(), end_time.date() - timedelta(days=1),
                require_all=False,
            )
            result = {"status": "success", "data": frame.to_dict("records")}
        else:
            # VISUALIZER: daily bars, the resolution the simulation feeds the
            # indicator next. Raw market_data rows (hourly in the default
            # store) made a period count hours in warmup and days afterwards.
            # Imported here because engine.core.utils imports this module.
            from engine.core import utils as core_utils

            frame = core_utils._fetch_from_db(
                self, [ticker],
                datetime.combine(start_time.date(), time.min, tzinfo=_NY_TZ),
                datetime.combine(end_time.date(), time.min, tzinfo=_NY_TZ),
            )
            result = {"status": "success", "data": frame.to_dict("records")}

        price_col: str = kwargs.get("price_col") or kwargs.get("close_col", "close_price")
        if result["status"] == "success" and result.get("data"):
            df = pd.DataFrame(result["data"])

            # --- Correctly handle timezone-aware data from the database ---
            # 1. Convert to UTC to create a standard, timezone-aware index.
            # 2. Convert back to 'America/New_York' to preserve the desired timezone info.
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

            numeric_cols = {price_col}
            for col_name in (
                kwargs.get("vol_col"),
                kwargs.get("high_col"),
                kwargs.get("low_col"),
                kwargs.get("close_col"),
            ):
                if col_name:
                    numeric_cols.add(col_name)

            for col_name in numeric_cols:
                if col_name in df.columns:
                    df[col_name] = pd.to_numeric(df[col_name], errors="coerce")

            dropna_cols = ["timestamp"]
            if price_col not in df.columns:
                self.logger.warning(
                    "Indicator warmup missing price_col for _build_indicator_update_payload path "
                    + "(indicator=%s, ticker=%s, price_col=%s, df_columns=%s)",
                    indicator_class_name,
                    ticker,
                    price_col,
                    list(df.columns),
                )
            if price_col in df.columns:
                dropna_cols.append(price_col)
            df.dropna(subset=dropna_cols, inplace=True)
            df.sort_values("timestamp", inplace=True)

            for row in df.itertuples():
                self._update_indicator_from_row(indicator, row)

        self._indicators.append(indicator)
        return indicator

    def RegisterIndicatorSet(self,
        indicator_definitions: dict[str, tuple[str, dict[str, Any]]]
    ) -> None:
        """
        Initializes a set of indicators for every ticker and attaches them as
        ticker-keyed dictionaries to the strategy instance. This is the
        recommended way to reduce __init__ boilerplate.

        Args:
            indicator_definitions (dict): A dictionary where keys are the desired
                attribute names (e.g., 'fast_sma') and values are a tuple of
                (IndicatorClassName, {**kwargs}).

                Example:
                {
                    "fast_sma": ("SimpleMovingAverage", {"period": 10}),
                    "slow_sma": ("SimpleMovingAverage", {"period": 30})
                }
        """
        for attr_name, (class_name, kwargs) in indicator_definitions.items():
            # Create a dictionary to hold this indicator for all tickers
            indicator_dict = {
                ticker: self.AddIndicator(class_name, ticker=ticker, **kwargs)
                for ticker in self.tickers
            }

            # Attach the completed dictionary as an attribute (e.g., self.fast_sma)
            setattr(self, attr_name, indicator_dict)
            self.logger.info(f"Registered indicator set '{attr_name}' for all tickers.")

    def generate_signals_and_trade(
        self, data: dict[str, pd.DataFrame], current_time: datetime | None = None
    ):
        """
        (Framework-Internal Method)
        Updates indicators with ALL new data points, constructs the context,
        and calls the user's OnData method.
        """
        market_data_df = data.get("MARKET_DATA")
        if market_data_df is not None and not market_data_df.empty:
            # --- Find ALL new bars since the last update ---
            # Guard against comparing a datetime series to None.
            if self._last_processed_timestamp is not None:
                new_data = market_data_df[
                    market_data_df["timestamp"] > self._last_processed_timestamp
                ]
            else:
                # On the first run, process only the single latest point to set a baseline
                new_data = (
                    market_data_df.sort_values("timestamp")
                    .groupby("ticker")
                    .last()
                    .reset_index()
                )

            # Process each new bar in chronological order for each ticker
            if not new_data.empty:
                for _, group in new_data.sort_values("timestamp").groupby("timestamp"):
                    for row in group.itertuples():
                        for indicator in self._indicators:
                            if not indicator.ticker == row.ticker:
                                continue
                            if not self._update_indicator_from_row(indicator, row):
                                continue

                            price_col = (
                                getattr(indicator, "price_col", None)
                                or getattr(indicator, "close_col", None)
                                or "close_price"
                            )
                            vol_col = getattr(indicator, "vol_col", "volume")
                            high_col = getattr(indicator, "high_col", "high_price")
                            low_col = getattr(indicator, "low_col", "low_price")
                            self.logger.debug(
                                "Updated %s for %s at %s: %s, vol=%s, high=%s, low=%s",
                                indicator.__class__.__name__,
                                row.ticker,
                                row.timestamp,
                                getattr(row, price_col),
                                getattr(row, vol_col, 'N/A'),
                                getattr(row, high_col, 'N/A'),
                                getattr(row, low_col, 'N/A')
                            )

        # Update the last processed time. If current_time is None, fall back to newest timestamp.
        if current_time is not None:
            self._last_processed_timestamp = current_time
        else:
            if market_data_df is not None and not market_data_df.empty:
                try:
                    self._last_processed_timestamp = market_data_df["timestamp"].max()
                except Exception:
                    self.logger.warning(
                        "Could not update last processed timestamp from market data."
                    )

        context = StrategyContext(
            market_data_df=market_data_df,
            cash_df=data.get("CASH_EQUITY"),
            positions_df=data.get("POSITIONS"),
            port_notional_df=data.get("PORT_NOTIONAL"),
            current_time=current_time,
            executor=self.executor,
            portfolio_config=self.portfolio_config_dict,
            order_manager=self.order_manager,
            )

        self.OnData(context)

    @abstractmethod
    def OnData(self, context: StrategyContext):
        """
        (User-Facing Method)
        This is the primary method that all user-defined strategies must implement.
        It is called by the framework on each time step (or polling interval)
        and provides a powerful context object with all necessary market and
        portfolio information and tools.

        Args:
            context (StrategyContext): The stateful API object for this point in time.
        """
        pass

    # --- Data Fetching Logic (Largely Unchanged) ---
    # The methods below are still required for the base class to function,
    # as it's responsible for fetching the data that will eventually be
    # passed into the StrategyContext.

    ATOMIC_STATE_QUERY: str = """
    WITH latest_cash AS (
        SELECT *
        FROM cash_equity_book
        WHERE portfolio_id = %s
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    ),
    latest_positions AS (
        SELECT DISTINCT ON (ticker)
            position_id, portfolio_id, ticker, quantity, updated_at
        FROM positions_book
        WHERE portfolio_id = %s
        ORDER BY ticker, updated_at DESC
    )
    SELECT
        (SELECT row_to_json(lc) FROM latest_cash lc) AS cash_data,
        (SELECT json_agg(lp) FROM latest_positions lp) AS positions_data;
    """

    MARKET_DATA_QUERY: str = """
        SELECT *
        FROM market_data
        WHERE ticker IN ({placeholders})
          AND timestamp BETWEEN %s AND %s
    """

    LATEST_PNL_QUERY: str = """
        SELECT *
        FROM pnl_book
        WHERE portfolio_id = %s
        ORDER BY timestamp DESC
        LIMIT 1
    """

    SEED_POSITION_QUERY: str = """
        INSERT INTO positions_book (portfolio_id, ticker, quantity)
        VALUES (%s, %s, 0)
        RETURNING *;
    """

    SEED_CASH_QUERY: str = """
        INSERT INTO cash_equity_book (portfolio_id, timestamp, date, currency, notional)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *;
    """

    # Default initial capital for auto-seeding (can be overridden in config)
    DEFAULT_INITIAL_CAPITAL: float = 1000000.0

    # VISUALIZER: everything from here down is the LIVE-TRADING data path
    # (cash_equity_book / positions_book / pnl_book). A backtest never
    # reaches it — BacktestRunner feeds the strategy from
    # BacktestExecutor.get_data_feeds() instead — and this application is
    # forbidden from touching those tables. Kept verbatim so the vendored
    # file stays diffable against MQSMaster.
    def get_data(self, data_feeds: list[str]) -> dict[str, pd.DataFrame]:
        """
        Fetches a consistent snapshot of portfolio data. Core state (cash, positions)
        is fetched atomically to prevent race conditions.
        """
        data = {feed: pd.DataFrame() for feed in data_feeds}

        try:
            params = (self.portfolio_id, self.portfolio_id)
            state_result = self.db.execute_query(
                self.ATOMIC_STATE_QUERY, params, fetch="one"
            )

            if state_result["status"] == "success" and state_result.get("data"):
                result_data = state_result["data"][0]
                if "CASH_EQUITY" in data_feeds:
                    if result_data.get("cash_data"):
                        data["CASH_EQUITY"] = pd.DataFrame([result_data["cash_data"]])
                    else:
                        # Auto-seed initial cash if none exists
                        data["CASH_EQUITY"] = self._seed_initial_cash()
                if "POSITIONS" in data_feeds and result_data.get("positions_data"):
                    data["POSITIONS"] = pd.DataFrame(result_data["positions_data"])
        except Exception as e:
            self.logger.exception(
                f"Failed to fetch atomic state for portfolio {self.portfolio_id}: {e}"
            )

        if "POSITIONS" in data_feeds:
            existing_tickers = (
                set(data["POSITIONS"]["ticker"])
                if not data["POSITIONS"].empty
                else set()
            )
            missing_tickers = set(self.tickers) - existing_tickers
            if missing_tickers:
                # The _seed_missing_positions method returns a new DataFrame
                data["POSITIONS"] = self._seed_missing_positions(
                    data["POSITIONS"], missing_tickers
                )

        if "MARKET_DATA" in data_feeds:
            data["MARKET_DATA"] = self._get_market_data()

        if "PORT_NOTIONAL" in data_feeds:
            data["PORT_NOTIONAL"] = self._get_portfolio_notional(
                fallback_cash_df=data.get("CASH_EQUITY")
            )

        return data

    def _seed_initial_cash(self) -> pd.DataFrame:
        """Auto-seed initial cash for portfolio if no cash equity records exist."""
        import pytz

        timezone = pytz.timezone("America/New_York")
        initial_capital = self.initial_capital
        timestamp = datetime.now(timezone)
        date_part = timestamp.date()

        self.logger.warning(
            f"No cash equity found for portfolio {self.portfolio_id}. "
            f"Auto-seeding with ${initial_capital:,.2f} initial capital."
        )

        try:
            result: dict[str, Any] = self.db.execute_query(
                self.SEED_CASH_QUERY,
                (self.portfolio_id, timestamp, date_part, "USD", initial_capital),
                fetch="all",
            )
            if result.get("status") == "success" and result.get("data"):
                self.logger.info(
                    f"Successfully seeded ${initial_capital:,.2f} for portfolio {self.portfolio_id}"
                )
                return pd.DataFrame(result["data"])
        except Exception as e:
            self.logger.exception(
                f"Failed to seed initial cash for portfolio {self.portfolio_id}: {e}"
            )

        # Return empty DataFrame if seeding failed
        return pd.DataFrame()

    def _seed_missing_positions(
        self, positions_df: pd.DataFrame, missing_tickers: set
    ) -> pd.DataFrame:
        """Helper to insert zero-quantity rows for tickers without a position record."""
        self.logger.info(
            f"Seeding zero-quantity positions for missing tickers: {missing_tickers}"
        )
        seeded_rows = []
        for ticker in missing_tickers:
            try:
                res = self.db.execute_query(
                    self.SEED_POSITION_QUERY, (self.portfolio_id, ticker), fetch="all"
                )
                if res.get("data"):
                    seeded_rows.extend(res["data"])
            except Exception as e:
                self.logger.exception(
                    f"Exception while seeding position for {ticker}: {e}"
                )

        if seeded_rows:
            seeded_df = pd.DataFrame(seeded_rows)
            # Ensure columns match before concatenating to avoid issues
            if not positions_df.empty:
                seeded_df = seeded_df[
                    positions_df.columns.intersection(seeded_df.columns)
                ]
            return pd.concat([positions_df, seeded_df], ignore_index=True)
        return positions_df

    def _get_market_data(self) -> pd.DataFrame:
        """Fetches recent market data for all portfolio tickers."""
        if not self.tickers:
            return pd.DataFrame()

        end_time = datetime.now()
        start_time = end_time - timedelta(days=self.lookback_days)

        placeholders = ", ".join(["%s"] * len(self.tickers))
        sql = self.MARKET_DATA_QUERY.format(placeholders=placeholders)
        params = self.tickers + [start_time.date(), end_time.date()]

        result = self.db.execute_query(sql, params, fetch="all")

        if result["status"] != "success" or not result.get("data"):
            return pd.DataFrame()

        df = pd.DataFrame(result["data"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["close_price"] = pd.to_numeric(df["close_price"])
        # Add other price columns to numeric conversion for robustness
        for col in ["open_price", "high_price", "low_price", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df.dropna(subset=["timestamp", "ticker", "close_price"], inplace=True)
        df.sort_values("timestamp", inplace=True)
        return df

    def _get_portfolio_notional(
        self, fallback_cash_df: pd.DataFrame | None = None
    ) -> pd.DataFrame:
        """
        Retrieves the latest portfolio notional. Falls back to cash if no PnL record exists.
        """
        pnl_result = self.db.execute_query(
            self.LATEST_PNL_QUERY, (self.portfolio_id,), fetch="one"
        )

        if pnl_result.get("status") == "success" and pnl_result.get("data"):
            return pd.DataFrame(pnl_result["data"])

        self.logger.info(
            f"No pnl_book entry for portfolio {self.portfolio_id}; using cash balance as notional."
        )
        if fallback_cash_df is not None and not fallback_cash_df.empty:
            return fallback_cash_df[["timestamp", "notional"]].copy()

        self.logger.warning(
            f"Fallback cash is also empty for portfolio {self.portfolio_id}; returning zero notional."
        )
        return pd.DataFrame([{"timestamp": datetime.now(), "notional": 0.0}])
