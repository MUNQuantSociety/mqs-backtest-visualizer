import logging
import math
from collections import namedtuple
from typing import Dict, List

import pandas as pd

from engine.core.cost_model import CostModel

# Result of the default sizing model: the share quantity to trade, the signed
# desired notional (its sign drives BUY vs SELL settlement), and the execution
# price used. Shared by the OMS path (reads .quantity) and execute_trade.
Sizing = namedtuple("Sizing", ["quantity", "desired_notional", "exec_price"])


class BacktestExecutor:
    """
    A backtest executor that manages a single, unified portfolio,
    supporting long/short positions with a realistic margin model that mirrors live trading constraints.
    """

    def __init__(
        self,
        initial_capital: float,
        tickers: List[str],
        leverage: float = 2.0,
        slippage: float = 0.0,
        cost_model: CostModel | None = None,
        adv_lookup: Dict[str, float] | None = None,
        sigma_lookup: Dict[str, float] | None = None,
        commission_per_share: float = 0.0,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.tickers = tickers
        self.leverage = leverage
        self.slippage = slippage
        # Legacy constant slippage stays available as a fallback (when cost_model is None).
        self.cost_model: CostModel | None = cost_model
        # VISUALIZER: explicit per-share commission is a cash fee, not a
        # price adjustment. Zero keeps the legacy sizing/settlement amounts.
        self.commission_per_share = float(commission_per_share)
        if not math.isfinite(self.commission_per_share) or self.commission_per_share < 0:
            raise ValueError("commission_per_share must be finite and nonnegative.")
        self.adv_lookup: Dict[str, float] = dict(adv_lookup or {})
        self.sigma_lookup: Dict[str, float] = dict(sigma_lookup or {})

        # --- Unified Portfolio State ---
        self.cash = initial_capital
        self.positions: Dict[str, float] = {ticker: 0.0 for ticker in tickers}
        self.latest_prices: Dict[str, float] = {ticker: 0.0 for ticker in tickers}
        self.trade_log: List[Dict] = []

        self.logger.info(
            "BacktestExecutor initialized with %.2f capital, leverage=%.2f, slippage=%.6f, "
            "commission_per_share=%.6f, cost_model=%s, for tickers: %s",
            initial_capital,
            leverage,
            slippage,
            self.commission_per_share,
            "on" if self.cost_model is not None else "off",
            tickers,
        )

    def _apply_slippage(
        self,
        price: float,
        signal_type: str,
        ticker: str | None = None,
        trade_notional: float = 0.0,
    ) -> float:
        """
        Applies execution cost to the price.
        If a CostModel is configured, uses it (fixed + spread + sqrt impact).
        Otherwise falls back to the legacy constant-multiplier slippage.
        """
        if self.cost_model is not None and ticker is not None:
            adv = float(self.adv_lookup.get(ticker, 0.0))
            sigma = float(self.sigma_lookup.get(ticker, 0.0))
            return self.cost_model.apply_to_price(
                mid_price=price,
                side=signal_type,
                trade_notional=abs(trade_notional),
                adv_notional=adv,
                sigma_daily=sigma,
                ticker=ticker,
            )
        if signal_type == "BUY":
            return price * (1 + self.slippage)
        elif signal_type == "SELL":
            return price * (1 - self.slippage)
        return price

    def update_price(self, ticker: str, price: float):
        """Updates the latest known price for a ticker."""
        if ticker in self.latest_prices:
            self.latest_prices[ticker] = price

    def get_port_notional(self) -> float:
        """Calculates the total current equity of the portfolio."""
        positions_value = sum(
            self.positions[ticker] * self.latest_prices.get(ticker, 0.0)
            for ticker in self.tickers
        )
        return self.cash + positions_value

    def get_position_value(self, ticker: str) -> float:
        """Calculates the notional value of a single ticker's position."""
        return self.positions.get(ticker, 0.0) * self.latest_prices.get(ticker, 0.0)

    def get_data_feeds(self) -> Dict[str, pd.DataFrame]:
        """Generates the portfolio state dataframes required by the strategy."""
        cash_df = pd.DataFrame([{"notional": self.cash}])
        positions_list = [
            {"ticker": ticker, "quantity": quantity}
            for ticker, quantity in self.positions.items()
        ]
        positions_df = pd.DataFrame(positions_list)
        port_notional_df = pd.DataFrame([{"notional": self.get_port_notional()}])

        return {
            "CASH_EQUITY": cash_df,
            "POSITIONS": positions_df,
            "PORT_NOTIONAL": port_notional_df,
        }

    def _calculate_buying_power(self, portfolio_equity: float) -> float:
        """Calculates the available buying power based on a margin model."""
        gross_position_value = sum(
            abs(self.positions[ticker] * self.latest_prices.get(ticker, 0.0))
            for ticker in self.tickers
        )
        buying_power = (portfolio_equity * self.leverage) - gross_position_value
        return max(0, buying_power)

    def default_trade_size(
        self,
        portfolio_id,
        signal_type,
        ticker,
        arrival_price,
        confidence,
        cash,
        positions,
        port_notional,
        ticker_weight,
        *,
        target_weight=None,
    ):
        """Size a trade with the default target-weight model, without filling.

        ``portfolio_id`` is accepted for signature parity with the live executor
        (where it keys the RBP overlay); the backtest has no overlay and ignores
        it. Single sizing entry point for both the OMS path (which reads
        ``.quantity`` to register a parent order) and ``execute_trade`` (which
        also needs ``.desired_notional`` for BUY/SELL direction and
        ``.exec_price`` for the fill). Returns a ``Sizing`` with ``quantity == 0``
        on any no-trade. ``cash``/``positions`` are accepted for signature parity
        with the live executor; backtest sizes against its own ``self`` state.
        ``target_weight`` explicitly selects signed final exposure (zero means
        flat). When omitted, the legacy signal/ticker_weight model is retained.
        """
        try:
            port_notional = float(port_notional)
            arrival_price = float(arrival_price)
            if target_weight is not None and not math.isfinite(float(confidence)):
                raise ValueError("Explicit target confidence must be finite")
            confidence = max(0.0, min(1.0, float(confidence)))
            ticker_weight = float(ticker_weight)
            if target_weight is not None:
                target_weight = float(target_weight)
                if not all(math.isfinite(value) for value in (
                    target_weight, port_notional, arrival_price, float(confidence)
                )):
                    raise ValueError("Explicit target inputs must be finite")
        except (ValueError, TypeError) as e:
            self.logger.error(f"Numeric conversion failed in default_trade_size: {e}")
            return Sizing(0, 0.0, 0.0)

        signal_type = signal_type.upper()
        if signal_type not in ("BUY", "SELL", "HOLD"):
            self.logger.warning(
                f"Invalid signal type '{signal_type}' for {ticker}. Must be BUY, SELL, or HOLD."
            )
            return Sizing(0, 0.0, 0.0)
        if signal_type == "HOLD" or confidence == 0.0:
            self.logger.debug(
                "Skip trade: signal=%s confidence=%.2f", signal_type, confidence
            )
            return Sizing(0, 0.0, 0.0)

        # If ticker_weight is 0 (no current position), default to equal-weight.
        if target_weight is None and ticker_weight == 0.0:
            if not self.tickers:
                self.logger.error("No tickers list available for fallback allocation.")
                return Sizing(0, 0.0, 0.0)
            ticker_weight = 1.0 / len(self.tickers)

        current_quantity = self.positions.get(ticker, 0.0)
        target_notional = port_notional * (
            ticker_weight if target_weight is None else target_weight
        )
        # The strategy still chooses its signed target. An overweight long
        # can need a SELL even after a BUY signal (and vice versa for shorts).
        if target_weight is None and signal_type == "SELL":
            target_notional *= -1
        execution_side = (
            "BUY" if target_notional > current_quantity * arrival_price else "SELL"
        )

        # First-pass approximation of trade notional so the cost model can size impact.
        approx_notional = abs(port_notional * ticker_weight * confidence)
        if target_weight is not None:
            approx_notional = abs(target_notional - current_quantity * arrival_price) * confidence
        exec_price = self._apply_slippage(
            arrival_price, execution_side, ticker=ticker, trade_notional=approx_notional
        )
        if not math.isfinite(exec_price) or exec_price <= 0:
            self.logger.warning(
                f"Cannot size trade for {ticker}: invalid exec price {exec_price} after slippage."
            )
            return Sizing(0, 0.0, exec_price)

        buying_power = self._calculate_buying_power(port_notional)

        current_notional_value = current_quantity * exec_price
        desired_trade_notional = (target_notional - current_notional_value) * confidence
        if (desired_trade_notional > 0) != (execution_side == "BUY"):
            # The target is inside the execution spread: do not fill in the
            # opposite direction at a price calculated for this side.
            return Sizing(0, desired_trade_notional, exec_price)

        # Keep the legacy minimum; an explicit flatten may close a cheap share.
        if abs(desired_trade_notional) < 1.0 and target_weight != 0.0:
            self.logger.debug(
                "Skip trade: desired_notional too small (%.2f) for %s",
                desired_trade_notional,
                ticker,
            )
            return Sizing(0, desired_trade_notional, exec_price)

        if target_weight is not None:
            # Work in shares so an exact zero target closes every whole share,
            # without a notional division rounding a full exit down by one.
            desired_quantity = abs(target_notional / exec_price - current_quantity) * confidence
            quantity_to_trade = math.floor(min(
                desired_quantity,
                self._commission_quantity_limit(
                    exec_price, execution_side, port_notional,
                    current_quantity=current_quantity,
                ),
            ))
            return Sizing(max(0, quantity_to_trade), desired_trade_notional, exec_price)

        # For buys, we are also constrained by the actual cash available.
        if desired_trade_notional > 0:  # This is a BUY operation
            tradable_notional = min(
                abs(desired_trade_notional), buying_power, self.cash
            )
        else:  # This is a SELL/SHORT operation
            tradable_notional = min(abs(desired_trade_notional), buying_power)

        if tradable_notional < 1.0:
            return Sizing(0, desired_trade_notional, exec_price)

        quantity_to_trade = math.floor(tradable_notional / exec_price)
        if self.commission_per_share:
            quantity_to_trade = min(
                quantity_to_trade,
                math.floor(self._commission_quantity_limit(exec_price, execution_side, port_notional)),
            )
        if quantity_to_trade <= 0:
            return Sizing(0, desired_trade_notional, exec_price)

        return Sizing(quantity_to_trade, desired_trade_notional, exec_price)

    def _commission_quantity_limit(
        self, exec_price, side, portfolio_equity, *, current_quantity=0.0
    ):
        """Reserve fees within the existing cash and gross-buying-power limits.

        One dollar of cash commission reduces margin buying power by leverage
        dollars. Keep the target-weight calculation unchanged, and only cap
        shares when paying their fees would exceed these existing limits.
        """
        fee = self.commission_per_share
        buying_power = self._calculate_buying_power(portfolio_equity)
        closing_quantity = max(0.0, -current_quantity if side == "BUY" else current_quantity)
        # Closing exposure releases margin; only shares beyond flat consume new
        # exposure. Legacy sizing leaves current_quantity at zero and keeps its
        # original cap. Explicit targets and OMS fills supply the actual holding.
        closing_margin_cost = self.leverage * fee - exec_price
        if closing_margin_cost > 0 and closing_quantity * closing_margin_cost > buying_power:
            limit = buying_power / closing_margin_cost
        else:
            limit = (buying_power + 2 * closing_quantity * exec_price) / (
                exec_price + self.leverage * fee
            )
        if side == "BUY":
            limit = min(limit, max(0.0, self.cash) / (exec_price + fee))
        elif fee > exec_price:
            # Short-sale proceeds normally fund the fee. If a configured fee
            # exceeds proceeds, the difference must be affordable in cash.
            limit = min(limit, max(0.0, self.cash) / (fee - exec_price))
        return max(0.0, limit)

    def execute_trade(
        self,
        portfolio_id,
        ticker,
        signal_type,
        confidence,
        arrival_price,
        cash,
        positions,
        port_notional,
        ticker_weight,
        timestamp,
        *,
        target_weight=None,
    ):
        # Size with the shared default model (handles coercion, signal/price
        # validation, equal-weight fallback, and buying-power/cash constraints).
        sizing = self.default_trade_size(
            portfolio_id=portfolio_id,
            signal_type=signal_type,
            ticker=ticker,
            arrival_price=arrival_price,
            confidence=confidence,
            cash=cash,
            positions=positions,
            port_notional=port_notional,
            ticker_weight=ticker_weight,
            target_weight=target_weight,
        )
        if sizing.quantity <= 0:
            return

        quantity_to_trade = sizing.quantity
        exec_price = sizing.exec_price
        # Record the settled side, which can differ from the target signal.
        # FIFO pairing must see the same direction as cash and positions.
        signal_type = "BUY" if sizing.desired_notional > 0 else "SELL"

        # --- Execute the Trade (settle the fill into the executor's own
        # unified portfolio state; the ``cash``/``positions`` params are kept
        # only for signature parity with the live executor). ---
        trade_value = quantity_to_trade * exec_price
        fees = quantity_to_trade * self.commission_per_share
        current_quantity = self.positions.get(ticker, 0.0)
        if sizing.desired_notional > 0:  # Finalizing a BUY
            self.cash -= trade_value
            self.positions[ticker] = current_quantity + quantity_to_trade
        else:  # Finalizing a SELL
            self.cash += trade_value
            self.positions[ticker] = current_quantity - quantity_to_trade
        self.cash -= fees

        self.trade_log.append(
            {
                "timestamp": timestamp,
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "confidence": max(0.0, min(1.0, float(confidence))),
                "shares": quantity_to_trade,
                "fill_price": exec_price,
                "fees": fees,
                "cash_after": self.cash,
                "position_size": self.positions.get(ticker, 0.0),
            }
        )

        return {
            "status": "success",
            "quantity": quantity_to_trade,
            "updated_cash": self.cash,
            "updated_quantity": self.positions.get(ticker, 0.0),
            "fees": fees,
            "fill_price": exec_price,
        }

    def execute_child_order(self, child_order, timestamp=None):
        """Settle one pre-sized OMS child order into the unified portfolio.

        The OMS execution seam (``OrderManager.manage_order`` calls this via
        its ``execute_child`` callable). Deliberately does NOT re-size: the
        parent was sized through ``default_trade_size`` (buying power, cash,
        RBP-blended confidence) when it was created; this method only fills
        the slice's fixed ``target_quantity`` at the *current* simulated
        price. Margin drift between parent sizing and slice fill is accepted
        v1 behavior when commission is zero. With per-share commission enabled,
        cash/margin affordability is checked again at the current fill price;
        an unaffordable fixed slice is rejected, never silently resized.

        Args:
            child_order: ``src.oms.order_structs.ChildOrder`` (duck-typed:
                needs ticker / signal_type / target_quantity / arrival_price
                / confidence / portfolio_id).
            timestamp: simulated time of the fill (the runner passes the
                event-loop timestamp so trade-log ordering matches sim time).

        Returns the OMS fill contract on success —
        ``{"status": "success", "filled_quantity": ..., "fill_price": ...}``
        — or an ``{"status": "error", ...}`` dict, which ``manage_order``
        routes to its retry-then-cancel path.
        """
        ticker = child_order.ticker
        signal_type = child_order.signal_type.value
        quantity = float(child_order.target_quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            return {"status": "error", "message": f"invalid quantity {quantity}"}
        if signal_type not in {"BUY", "SELL"}:
            return {"status": "error", "message": "child signal must be BUY or SELL"}

        price = self.latest_prices.get(ticker, 0.0)
        if price <= 0:
            # No price yet for this ticker at this point in the sim: report
            # failure so the OMS retries on a later bar rather than filling
            # at a bogus price.
            self.logger.warning(
                "No valid simulated price for %s; child %s not filled.",
                ticker,
                child_order.child_id,
            )
            return {"status": "error", "message": f"no price for {ticker}"}

        # Per-slice cost: each child pays impact on its own (smaller)
        # notional — this is exactly the benefit slicing is meant to show
        # under the sqrt-impact cost model.
        exec_price = self._apply_slippage(
            price, signal_type, ticker=ticker, trade_notional=quantity * price
        )
        if not math.isfinite(exec_price) or exec_price <= 0:
            return {"status": "error", "message": "invalid price after slippage"}
        if self.commission_per_share and quantity > self._commission_quantity_limit(
            exec_price, signal_type, self.get_port_notional(),
            current_quantity=self.positions.get(ticker, 0.0),
        ):
            return {"status": "error", "message": "insufficient cash or buying power including commission"}

        trade_value = quantity * exec_price
        fees = quantity * self.commission_per_share
        current_quantity = self.positions.get(ticker, 0.0)
        if signal_type == "BUY":
            self.cash -= trade_value
            self.positions[ticker] = current_quantity + quantity
        else:  # SELL
            self.cash += trade_value
            self.positions[ticker] = current_quantity - quantity
        self.cash -= fees

        # Same record shape as execute_trade so reporting treats OMS fills
        # identically; confidence is the parent's registered value.
        self.trade_log.append(
            {
                "timestamp": timestamp,
                "portfolio_id": child_order.portfolio_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "confidence": float(child_order.confidence),
                "shares": quantity,
                "fill_price": exec_price,
                "fees": fees,
                "cash_after": self.cash,
                "position_size": self.positions.get(ticker, 0.0),
            }
        )

        return {
            "status": "success",
            "filled_quantity": quantity,
            "fill_price": exec_price,
            "fees": fees,
        }

    def dump_trade_log(self) -> list[str]:
        """
        Generate formatted trade log entries and return them as a list of strings.
        """
        trade_logs: list[str] = []
        for entry in self.trade_log:
            ts = entry["timestamp"]
            ts_str = (
                ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            )
            msg = (
                f"[{entry.get('portfolio_id', 'unknown')}] "
                f"{ts_str} - "
                f"{entry['ticker']} | {entry['signal_type']} "
                f"{entry['shares']} @ {entry['fill_price']:.2f}$ "
                f"cash={entry['cash_after']:.2f}$"
                f" qty={entry['position_size']} "
            )
            trade_logs.append(msg)
        return trade_logs
