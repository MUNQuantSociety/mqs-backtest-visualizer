"""FIFO pairing of engine fills into round-trip trades.

The backtest engine only knows *fills*: one leg each, appended to
``BacktestExecutor.trade_log`` as they settle (a BUY now, a SELL later). The
frontend's trade table and P&L histogram consume *round trips* — one row with
``entryDate``/``exitDate``/``pnl``/``returnPct``. This module is that
translation, and nothing else: it is pure, has no imports from the rest of the
app, and touches no database.

It is deliberately boring. A wrong number here is not a visible bug — it is a
plausible-looking chart that quietly misrepresents a strategy, so every rule
below is spelled out rather than inferred.

Input contract
--------------
A fill is a mapping with the keys the engine's executor writes (see
``engine/core/executor.py``, ``execute_trade`` / ``execute_child_order``):

============== ============================================================
``timestamp``  ``datetime`` (tz-aware or naive), ``date``, pandas
               ``Timestamp``, or an ISO-8601 string. Required — a fill with
               no usable time cannot be dated, and a silently undated trade
               is worse than a loud failure.
``ticker``     symbol, becomes ``symbol`` on the output row
``signal_type````"BUY"`` or ``"SELL"`` (case-insensitive)
``shares``     positive quantity filled
``fill_price`` execution price, already slipped/costed by the engine
``fees``       optional; total cash fees for the whole fill, pro-rated across
               its shares. The engine does not emit this today (its cost
               model is baked into ``fill_price``), so it defaults to 0.
============== ============================================================

Everything else the executor logs (``portfolio_id``, ``confidence``,
``cash_after``, ``position_size``) is ignored: position state is re-derived
here from the fill sequence so the pairing cannot disagree with itself.

Pairing rules
-------------
* **FIFO, per ticker.** Tickers are independent books; within a ticker the
  oldest open lot closes first.
* A BUY opens or extends a long; a SELL closes the oldest open long lots and
  any excess opens a short. Mirrored for shorts.
* **Partial fills split lots.** Each *closed lot* emits exactly one round
  trip, so a 100-share entry closed in two 50-share exits produces two rows.
* ``pnl = (exit_price - entry_price) * quantity`` for longs and the negation
  of that for shorts, so a profitable short is positive.
* ``return_pct = pnl / (entry_price * quantity)`` — a ratio (0.043 = +4.3%),
  matching the frontend's ``returnPct``. Zero when the entry notional is
  zero, which only happens with a nonsensical zero price.
* ``pnl`` is **gross of fees**. ``fees`` is reported alongside it (entry
  share + exit share) and the UI subtracts if it wants to; folding fees into
  ``pnl`` would silently break ``pnl == (exit - entry) * qty``, the identity
  anyone eyeballing the table will check.

Lots still open when the fills run out
--------------------------------------
A backtest that ends holding a position leaves lots unclosed. Those still
become rows — hiding them would make the trade count disagree with the
position the equity curve is carrying — with ``exit_date=None``,
``exit_price=None`` and **``pnl=0.0``, ``return_pct=0.0``**. That is a
deliberate choice: the row reports *realised* P&L, and an open lot has
realised nothing. Marking it with unrealised P&L would require a mark price
this module has no business knowing, and would double-count against the final
equity the run already reports. Their ``fees`` still carry the entry-side
fees actually paid.

Ordering
--------
``seq`` is 0-based, contiguous, and deterministic for a given input:
round trips first, in the order they *closed* (FIFO within a closing fill),
then the unclosed lots in the order they *opened*. Fills are stable-sorted by
timestamp before pairing, so an input already in engine order is unchanged
and an interleaved one is still paired chronologically.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, Mapping, Sequence, TypedDict

__all__ = ["Fill", "TradeRow", "pair_fills"]

# Share counts arrive as floats; compare against a tolerance rather than 0 so
# that 100 - 33 - 33 - 34 closes a lot instead of leaving 7e-15 shares open.
_EPS = 1e-9

_LONG = 1
_SHORT = -1


class Fill(TypedDict, total=False):
    """One executor fill. Only the first five keys are required.

    Declared for documentation and type-checking; ``pair_fills`` accepts any
    mapping with these keys, which is what the engine actually hands over.
    """

    timestamp: Any
    ticker: str
    signal_type: str
    shares: float
    fill_price: float
    fees: float


@dataclass(frozen=True)
class TradeRow:
    """One round trip — the columns of ``app.run_trades`` minus ``run_id``.

    ``run_id`` is supplied by the persistence layer, which knows it; see
    ``as_row``.
    """

    seq: int
    symbol: str
    side: Literal["long", "short"]
    entry_date: str
    exit_date: str | None
    entry_price: float
    exit_price: float | None
    quantity: float
    pnl: float
    return_pct: float
    fees: float

    def as_row(self, run_id: Any) -> dict[str, Any]:
        """Return the full ``run_trades`` row, ready for a bulk insert."""
        return {
            "run_id": run_id,
            "seq": self.seq,
            "symbol": self.symbol,
            "side": self.side,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
            "fees": self.fees,
        }


@dataclass
class _Lot:
    """An open position slice awaiting a closing fill."""

    direction: int  # _LONG or _SHORT
    quantity: float  # always positive; the sign lives in ``direction``
    price: float
    day: str  # ISO date of the opening fill
    fee_per_share: float
    open_index: int  # opening order, for deterministic remainder ordering


@dataclass(frozen=True)
class _Fill:
    """A validated, normalised fill."""

    direction: int
    ticker: str
    quantity: float
    price: float
    day: str
    fee_per_share: float
    sort_key: tuple[datetime, int]


def pair_fills(fills: Sequence[Mapping[str, Any]]) -> list[TradeRow]:
    """Pair one-leg fills into round-trip trades, FIFO per ticker.

    Raises:
        ValueError: a fill is missing a required key, or carries a timestamp,
            side, quantity or price that cannot be interpreted. Bad input is
            surfaced rather than absorbed — a dropped fill unbalances every
            later pairing for that ticker, and the damage would only show up
            as a slightly wrong P&L column.
    """
    normalised = _normalise(fills)

    open_lots: defaultdict[str, deque[_Lot]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []
    open_index = 0

    for fill in normalised:
        lots = open_lots[fill.ticker]
        remaining = fill.quantity

        # Close opposing lots oldest-first; each fully or partially closed lot
        # is one round trip.
        while remaining > _EPS and lots and lots[0].direction != fill.direction:
            lot = lots[0]
            matched = min(remaining, lot.quantity)
            closed.append(_round_trip(lot, fill, matched))

            lot.quantity -= matched
            remaining -= matched
            if lot.quantity <= _EPS:
                lots.popleft()

        # Anything left over opens (or extends) a position in the fill's own
        # direction — this is the long-to-short flip when a single SELL is
        # larger than the open long.
        if remaining > _EPS:
            lots.append(
                _Lot(
                    direction=fill.direction,
                    quantity=remaining,
                    price=fill.price,
                    day=fill.day,
                    fee_per_share=fill.fee_per_share,
                    open_index=open_index,
                )
            )
            open_index += 1

    rows = closed + [_open_row(lot, ticker) for ticker, lot in _with_ticker(open_lots)]

    return [TradeRow(seq=seq, **row) for seq, row in enumerate(rows)]


def _with_ticker(
    open_lots: Mapping[str, deque[_Lot]],
) -> list[tuple[str, _Lot]]:
    """Remaining lots paired with their ticker, in the order they opened."""
    pairs = [(ticker, lot) for ticker, lots in open_lots.items() for lot in lots]
    pairs.sort(key=lambda pair: pair[1].open_index)
    return pairs


def _round_trip(lot: _Lot, fill: _Fill, quantity: float) -> dict[str, Any]:
    """Build one closed round trip from a lot and the fill closing it."""
    # ``direction`` flips the sign so a short that exits below its entry is a
    # gain, not a loss.
    pnl = (fill.price - lot.price) * quantity * lot.direction
    entry_notional = lot.price * quantity
    return {
        "symbol": fill.ticker,
        "side": "long" if lot.direction == _LONG else "short",
        "entry_date": lot.day,
        "exit_date": fill.day,
        "entry_price": lot.price,
        "exit_price": fill.price,
        "quantity": quantity,
        "pnl": pnl,
        "return_pct": pnl / entry_notional if entry_notional else 0.0,
        "fees": quantity * (lot.fee_per_share + fill.fee_per_share),
    }


def _open_row(lot: _Lot, ticker: str) -> dict[str, Any]:
    """Build the row for a lot that never got a closing fill (see module doc)."""
    return {
        "symbol": ticker,
        "side": "long" if lot.direction == _LONG else "short",
        "entry_date": lot.day,
        "exit_date": None,
        "entry_price": lot.price,
        "exit_price": None,
        "quantity": lot.quantity,
        "pnl": 0.0,
        "return_pct": 0.0,
        "fees": lot.quantity * lot.fee_per_share,
    }


def _normalise(fills: Sequence[Mapping[str, Any]]) -> list[_Fill]:
    """Validate every fill and stable-sort them chronologically."""
    normalised: list[_Fill] = []

    for index, raw in enumerate(fills):
        quantity = _number(raw, "shares", index)
        if quantity <= _EPS:
            # A zero-share fill moves no position and has no price impact; the
            # executor does not log one, but a hand-built log might.
            continue

        moment = _timestamp(raw, index)
        # An absent or explicitly null fee column means "no fees recorded",
        # which is what the engine's own log looks like today.
        fees = 0.0 if raw.get("fees") is None else _number(raw, "fees", index)
        normalised.append(
            _Fill(
                direction=_direction(raw, index),
                ticker=_ticker(raw, index),
                quantity=quantity,
                price=_number(raw, "fill_price", index),
                day=moment.date().isoformat(),
                fee_per_share=fees / quantity,
                sort_key=(_comparable(moment), index),
            )
        )

    normalised.sort(key=lambda fill: fill.sort_key)
    return normalised


def _ticker(raw: Mapping[str, Any], index: int) -> str:
    value = raw.get("ticker")
    if not isinstance(value, str) or not value:
        raise ValueError(f"fill {index}: missing or empty 'ticker' ({value!r})")
    return value


def _direction(raw: Mapping[str, Any], index: int) -> int:
    value = raw.get("signal_type")
    side = value.upper() if isinstance(value, str) else value
    if side == "BUY":
        return _LONG
    if side == "SELL":
        return _SHORT
    raise ValueError(f"fill {index}: 'signal_type' must be BUY or SELL, got {value!r}")


def _number(raw: Mapping[str, Any], key: str, index: int) -> float:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"fill {index}: missing {key!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"fill {index}: {key!r} is not numeric ({value!r})") from exc


def _timestamp(raw: Mapping[str, Any], index: int) -> datetime:
    """Coerce a fill timestamp to a datetime. Pandas ``Timestamp`` is a
    ``datetime`` subclass, so it needs no special case."""
    value = raw.get("timestamp")
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"fill {index}: 'timestamp' is not ISO-8601 ({value!r})"
            ) from exc
    raise ValueError(f"fill {index}: unusable 'timestamp' ({value!r})")


def _comparable(moment: datetime) -> datetime:
    """Naive-UTC view of a timestamp, so a mixed log still sorts.

    Comparing an aware and a naive datetime raises; the engine emits
    America/New_York-aware timestamps but hand-built or replayed logs may not.
    The *reported* date always comes from the original timestamp — only the
    sort key is normalised.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
