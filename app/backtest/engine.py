from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

from app.backtest.fills import (
    BASIS_POINTS,
    ZERO,
    available_quantity,
    execution_price,
    protective_trigger,
    round_down,
)
from app.backtest.metrics import metrics_for
from app.backtest.models import (
    EquityPoint,
    Fill,
    Order,
    Side,
    SimulationResult,
    SimulationSettings,
    Trade,
)
from app.market.indicators import INDICATOR_CONTEXT
from app.market.models import Candle
from app.strategies.engine import Replay
from app.strategies.models import Position, Regime, Signal

type SignalAt = Callable[[datetime, Position], Signal]


@dataclass(frozen=True)
class Intent:
    side: Side
    decision_at: datetime
    trigger: str
    signal_id: str | None = None
    stop: Decimal | None = None
    target: Decimal | None = None


@dataclass
class Holding:
    opened_at: datetime
    original_quantity: Decimal
    quantity: Decimal
    entry_cost: Decimal
    remaining_cost: Decimal
    equity_before: Decimal
    stop: Decimal | None
    target: Decimal | None
    realized: Decimal = ZERO


class Portfolio:
    def __init__(self, settings: SimulationSettings) -> None:
        self.settings = settings
        self.cash = settings.initial_cash_inr
        self.realized = ZERO
        self.holding: Holding | None = None
        self.orders: list[Order] = []
        self.trades: list[Trade] = []

    def record(
        self,
        intent: Intent,
        status: Literal["FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"],
        *,
        reason: str | None = None,
        requested: Decimal | None = None,
        fill: Fill | None = None,
    ) -> None:
        self.orders.append(
            Order(
                order_id=len(self.orders) + 1,
                side=intent.side,
                decision_at=intent.decision_at,
                signal_id=intent.signal_id,
                trigger=intent.trigger,
                status=status,
                reason=reason,
                requested_btc=requested,
                fill=fill,
            )
        )

    def execute(
        self,
        intent: Intent,
        reference: Decimal,
        timestamp: datetime,
        capacity: Decimal,
        execution: Literal["OPEN", "INTRABAR"],
    ) -> Decimal:
        settings = self.settings
        buy = intent.side == "BUY"
        price = execution_price(reference, intent.side, settings)
        if price <= 0:
            self.record(intent, "REJECTED", reason="INVALID_EXECUTION_PRICE")
            return ZERO
        fee_rate = (settings.buy_fee_bps if buy else settings.sell_fee_bps) / BASIS_POINTS
        max_order = round_down(settings.max_order_btc, settings.quantity_step_btc)

        if buy:
            if self.holding is not None:
                self.record(intent, "REJECTED", reason="POSITION_ALREADY_LONG")
                return ZERO
            if (intent.stop is not None and price <= intent.stop) or (
                intent.target is not None and price >= intent.target
            ):
                self.record(intent, "REJECTED", reason="ENTRY_OUTSIDE_BRACKET")
                return ZERO
            budget_quantity = self.cash * settings.allocation / (price * (1 + fee_rate))
            requested = min(round_down(budget_quantity, settings.quantity_step_btc), max_order)
        else:
            if self.holding is None:
                self.record(intent, "REJECTED", reason="POSITION_FLAT")
                return ZERO
            requested = min(self.holding.quantity, max_order)

        if requested < settings.min_quantity_btc or requested * price < settings.min_notional_inr:
            self.record(intent, "REJECTED", reason="BELOW_ORDER_MINIMUM", requested=requested)
            return ZERO
        quantity = min(requested, capacity)
        if quantity < settings.min_quantity_btc or quantity * price < settings.min_notional_inr:
            self.record(
                intent, "CANCELLED", reason="INSUFFICIENT_PRIOR_VOLUME", requested=requested
            )
            return ZERO

        notional = price * quantity
        fee = notional * fee_rate
        if buy:
            cost = notional + fee
            self.holding = Holding(
                timestamp, quantity, quantity, cost, cost, self.cash, intent.stop, intent.target
            )
            self.cash -= cost
        else:
            self._sell(quantity, notional - fee, timestamp)
        self.record(
            intent,
            "FILLED" if quantity == requested else "PARTIALLY_FILLED",
            reason="VOLUME_CAP" if quantity < requested else None,
            requested=requested,
            fill=Fill(
                timestamp=timestamp,
                execution=execution,
                quantity_btc=quantity,
                reference_price_inr=reference,
                price_inr=price,
                notional_inr=notional,
                fee_inr=fee,
                spread_slippage_inr=abs(price - reference) * quantity,
                cash_after_inr=self.cash,
                position_after_btc=self.holding.quantity if self.holding is not None else ZERO,
            ),
        )
        return quantity

    def _sell(self, quantity: Decimal, proceeds: Decimal, timestamp: datetime) -> None:
        holding = self.holding
        assert holding is not None
        basis = (
            holding.remaining_cost
            if quantity == holding.quantity
            else holding.remaining_cost * quantity / holding.quantity
        )
        pnl = proceeds - basis
        self.cash += proceeds
        self.realized += pnl
        holding.realized += pnl
        holding.remaining_cost -= basis
        holding.quantity -= quantity
        if holding.quantity == 0:
            self.trades.append(
                Trade(
                    opened_at=holding.opened_at,
                    closed_at=timestamp,
                    quantity_btc=holding.original_quantity,
                    entry_cost_inr=holding.entry_cost,
                    net_pnl_inr=holding.realized,
                    return_on_entry=holding.realized / holding.entry_cost,
                    return_on_equity=holding.realized / holding.equity_before,
                )
            )
            self.holding = None


def simulate(
    replay: Replay,
    settings: SimulationSettings,
    *,
    signal_at: SignalAt | None,
    start: datetime | None = None,
    end: datetime | None = None,
    candles: Sequence[Candle] | None = None,
) -> SimulationResult:
    initial_time = replay.start if start is None else start
    final_time = replay.end if end is None else end
    candles = replay.candles if candles is None else candles
    with localcontext(INDICATOR_CONTEXT):
        portfolio = Portfolio(settings)
        peak = settings.initial_cash_inr
        equity = [
            EquityPoint(
                timestamp=initial_time,
                cash_inr=peak,
                position_btc=ZERO,
                mark_price_inr=None,
                equity_inr=peak,
                drawdown=ZERO,
            )
        ]
        pending = None
        unavailable = buys = sells = 0

        for candle in candles:
            capacity = available_quantity(replay.signal_bars.get(candle.open_time), settings)
            if signal_at is None and portfolio.holding is None:
                pending = Intent("BUY", candle.open_time, "BUY_AND_HOLD")
            if pending is not None:
                if pending.decision_at != candle.open_time:
                    portfolio.record(pending, "CANCELLED", reason="MISSING_NEXT_CANDLE")
                else:
                    capacity -= portfolio.execute(
                        pending, candle.open, candle.open_time, capacity, "OPEN"
                    )
                pending = None

            holding = portfolio.holding
            if holding is not None:
                trigger = protective_trigger(candle, holding.stop, holding.target)
                if trigger is not None:
                    reason, price, timestamp, execution = trigger
                    portfolio.execute(
                        Intent("SELL", timestamp, reason), price, timestamp, capacity, execution
                    )

            quantity = portfolio.holding.quantity if portfolio.holding is not None else ZERO
            mark = portfolio.cash + quantity * candle.close
            peak = max(peak, mark)
            equity.append(
                EquityPoint(
                    timestamp=candle.close_time,
                    cash_inr=portfolio.cash,
                    position_btc=quantity,
                    mark_price_inr=candle.close,
                    equity_inr=mark,
                    drawdown=(peak - mark) / peak,
                )
            )

            if signal_at is not None:
                position = Position.LONG if quantity > 0 else Position.FLAT
                signal = signal_at(candle.close_time, position)
                unavailable += signal.regime == Regime.DATA_UNAVAILABLE
                buys += signal.action == "BUY"
                sells += signal.action == "SELL"
                if (signal.action == "BUY" and position == Position.FLAT) or (
                    signal.action == "SELL" and position == Position.LONG
                ):
                    pending = Intent(
                        signal.action,
                        signal.timestamp,
                        "STRATEGY_SIGNAL",
                        signal.signal_id,
                        signal.stop_loss,
                        signal.take_profit,
                    )

        if pending is not None:
            reason = "MISSING_NEXT_CANDLE" if pending.decision_at < final_time else "END_OF_WINDOW"
            portfolio.record(pending, "CANCELLED", reason=reason)
        return SimulationResult(
            metrics=metrics_for(equity, portfolio.orders, portfolio.trades, portfolio.realized),
            orders=portfolio.orders,
            trades=portfolio.trades,
            equity_curve=equity,
            candles=len(candles),
            unavailable_signals=unavailable,
            buy_signals=buys,
            sell_signals=sells,
        )
