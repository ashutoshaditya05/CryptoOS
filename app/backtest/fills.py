from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from app.backtest.models import Side, SimulationSettings
from app.market.models import Candle

ZERO = Decimal(0)
BASIS_POINTS = Decimal(10_000)


def round_down(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def execution_price(reference: Decimal, side: Side, settings: SimulationSettings) -> Decimal:
    adjustment = (settings.spread_bps / 2 + settings.slippage_bps) / BASIS_POINTS
    tick = settings.price_tick_inr
    if side == "BUY":
        return (reference * (1 + adjustment) / tick).to_integral_value(
            rounding=ROUND_CEILING
        ) * tick
    return round_down(reference * (1 - adjustment), tick)


def available_quantity(previous: Candle | None, settings: SimulationSettings) -> Decimal:
    if previous is None or previous.volume is None:
        return ZERO
    return round_down(previous.volume * settings.volume_participation, settings.quantity_step_btc)


def protective_trigger(
    candle: Candle, stop: Decimal | None, target: Decimal | None
) -> tuple[str, Decimal, datetime, Literal["OPEN", "INTRABAR"]] | None:
    if stop is not None and candle.open <= stop:
        return "STOP_LOSS", candle.open, candle.open_time, "OPEN"
    if target is not None and candle.open >= target:
        return "TAKE_PROFIT", target, candle.open_time, "OPEN"
    # With no intrabar ordering, a touch of both levels is resolved against the long.
    if stop is not None and candle.low <= stop:
        return "STOP_LOSS", stop, candle.close_time, "INTRABAR"
    if target is not None and candle.high >= target:
        return "TAKE_PROFIT", target, candle.close_time, "INTRABAR"
    return None
