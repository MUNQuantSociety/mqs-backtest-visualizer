"""On-demand daily FMP history, independent of the application and DB cache.

Based on MQSMaster's FMPMarketData.get_historical_data contract, using FMP's
current stable endpoint. Environment access is intentional, as in db_adapter:
the standalone engine and spawned workers must read the same repository .env.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv

from engine.contracts.errors import EngineError, NoMarketData

logger = logging.getLogger(__name__)
ENDPOINT = "https://financialmodelingprep.com/stable/historical-price-eod/full"
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class FMPUnavailable(EngineError):
    """A provider/configuration failure, never an assertion of missing history."""


def market_data_source() -> str:
    load_dotenv(_ENV_FILE, override=False)
    source = os.getenv("MARKET_DATA_SOURCE", "").strip().lower() or "fmp"
    if source not in {"fmp", "database"}:
        raise FMPUnavailable("MARKET_DATA_SOURCE must be 'fmp' or 'database'.")
    return source


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class FMPMarketData:
    def __init__(self) -> None:
        load_dotenv(_ENV_FILE, override=False)
        self._api_key = os.getenv("FMP_API_KEY", "").strip()
        if not self._api_key:
            raise FMPUnavailable("FMP_API_KEY is missing. Set it in the backend .env and restart the API.")

    def _make_request(self, ticker: str, start: date, end: date) -> list[dict]:
        url = ENDPOINT + "?" + urlencode({
            "symbol": ticker, "from": start.isoformat(), "to": end.isoformat(),
            "apikey": self._api_key,
        })
        # Bound transient retries. Never log URLs, provider bodies or raw
        # exceptions: all can contain the query-string API key.
        for attempt in range(2):
            try:
                with urlopen(url, timeout=6) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if (code == 429 or code >= 500) and attempt == 0:
                    time.sleep(0.5)
                    continue
                advice = {
                    401: "Check FMP_API_KEY in the backend .env.",
                    402: "Check the FMP plan's historical-data access.",
                    403: "Check the API key and the FMP plan's historical-data access.",
                    429: "FMP's request limit was reached; retry shortly.",
                }.get(code, "Retry shortly or check FMP service availability.")
                raise FMPUnavailable(f"FMP history for {ticker} failed (HTTP {code}). {advice}") from None
            except (URLError, TimeoutError, OSError):
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise FMPUnavailable(f"FMP history for {ticker} could not be reached. Retry shortly.") from None
            except (ValueError, UnicodeError):
                raise FMPUnavailable(f"FMP returned invalid history for {ticker}. Retry shortly.") from None
            if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
                raise FMPUnavailable(
                    f"FMP returned an error or unexpected history for {ticker}. Check the API key and plan access."
                )
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    def get_historical_data(self, tickers, from_date, to_date) -> list[dict]:
        """Daily OHLCV records for each symbol; both exchange dates inclusive."""
        wanted = tickers.split(",") if isinstance(tickers, str) else tickers
        wanted = list(dict.fromkeys(str(t).strip().upper() for t in wanted if str(t).strip()))
        start, end = _day(from_date), _day(to_date)
        if start > end:
            raise ValueError("FMP history start must be on or before end.")
        rows = []
        for ticker in wanted:
            payload = self._make_request(ticker, start, end)
            for raw in payload:
                try:
                    day = date.fromisoformat(raw["date"])
                    if raw.get("symbol", ticker) != ticker:
                        raise ValueError("unexpected symbol")
                    prices = {f"{field}_price": float(raw[field]) for field in ("open", "high", "low", "close")}
                    volume = float(raw["volume"])
                    if any(not math.isfinite(p) or p <= 0 for p in prices.values()):
                        raise ValueError("invalid price")
                    if not math.isfinite(volume) or volume < 0:
                        raise ValueError("invalid volume")
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise FMPUnavailable(f"FMP returned an invalid daily bar for {ticker}. Retry shortly.") from None
                if start <= day <= end:
                    rows.append({"ticker": ticker, "date": day, **prices, "volume": volume})
            logger.info("FMP | Fetched daily history; ticker=%s window=%s..%s rows=%d", ticker, start, end, len(payload))
        return rows


def fetch_daily_history(tickers: list[str], start, end, *, require_all: bool = True):
    """Engine columns with daily bars labelled at 16:00 America/New_York.

    Always fetch the requested window, including warmup, from FMP. Existing
    database parquet files are deliberately outside this provider's path.
    """
    import pandas as pd

    wanted = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    rows = FMPMarketData().get_historical_data(wanted, start, end)
    present = {row["ticker"] for row in rows}
    missing = [ticker for ticker in wanted if ticker not in present]
    if missing and require_all:
        raise NoMarketData(missing, start, end, reason="FMP returned no daily bars in this window")
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["timestamp"] = (
        pd.to_datetime(frame.pop("date")) + pd.Timedelta(hours=16)
    ).dt.tz_localize("America/New_York")
    return frame.drop_duplicates(["ticker", "timestamp"]).sort_values(["timestamp", "ticker"]).reset_index(drop=True)


class FMPDataAdapter:
    """One run's FMP history, shared by warmup and simulation; no SQL access.

    Remember the requested bounds, including empty pre-IPO days, so a warmup
    slice cannot cause repeated downloads. Nothing is read from the DB cache
    or retained across runs. Longer indicator lookbacks extend this run's data.
    """

    def __init__(self) -> None:
        self._history = {}

    def get_daily_history(self, tickers, start, end, *, require_all=True):
        import pandas as pd

        wanted = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
        start, end = _day(start), _day(end)
        needed = []
        for ticker in wanted:
            cached = self._history.get(ticker)
            if cached is None or start < cached[0] or end > cached[1]:
                first = min(start, cached[0]) if cached else start
                last = max(end, cached[1]) if cached else end
                needed.append((ticker, first, last))

        def fetch(item):
            ticker, first, last = item
            frame = fetch_daily_history([ticker], first, last, require_all=False)
            return ticker, (first, last, frame)

        if needed:
            with ThreadPoolExecutor(max_workers=min(4, len(needed))) as pool:
                self._history.update(pool.map(fetch, needed))
        parts, missing = [], []
        for ticker in wanted:
            frame = self._history[ticker][2]
            if not frame.empty:
                days = frame["timestamp"].dt.date
                frame = frame[(days >= start) & (days <= end)]
            if frame.empty:
                missing.append(ticker)
            else:
                parts.append(frame)
        if missing and require_all:
            raise NoMarketData(missing, start, end, reason="FMP returned no daily bars in this window")
        if not parts:
            return pd.DataFrame()
        return pd.concat(parts, ignore_index=True).sort_values(["timestamp", "ticker"]).reset_index(drop=True)

    def execute_query(self, *args, **kwargs):
        raise FMPUnavailable(
            "Database price queries are disabled for FMP backtests. "
            "Use context.Market or the engine's FMP history."
        )

    def close(self):
        self._history.clear()
