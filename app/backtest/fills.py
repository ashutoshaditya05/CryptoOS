from decimal import Decimal

from app.backtest.models import (
    INR_PER_POINT_PER_LOT,
    MAINTENANCE_RATE,
    Bracket,
    Fill,
    ReplaySettings,
)
from app.market.orderbook import Level, Quote


def sweep(levels: tuple[Level, ...], contracts: int, cap_inr: Decimal | None = None) -> Fill | None:
    remaining = contracts
    notional = Decimal(0)
    for price, available in levels:
        unit_cost = price * INR_PER_POINT_PER_LOT
        count = min(remaining, available)
        if cap_inr is not None:
            count = min(count, int((cap_inr - notional) // unit_cost))
        if count <= 0:
            break
        notional += unit_cost * count
        remaining -= count
        if not remaining:
            break
    filled = contracts - remaining
    if not filled or (cap_inr is None and remaining):
        return None
    return Fill(filled, notional / (filled * INR_PER_POINT_PER_LOT), notional)


def buy(quote: Quote, settings: ReplaySettings) -> Fill | None:
    cap = settings.margin_inr * settings.leverage
    contracts = int(cap // (quote.ask * INR_PER_POINT_PER_LOT))
    return sweep(quote.asks, contracts, cap)


def bracket(fill: Fill, settings: ReplaySettings) -> Bracket | None:
    entry = fill.average_usd
    stop = entry - settings.stop_points
    liquidation = entry * (1 - Decimal(1) / settings.leverage) / (1 - MAINTENANCE_RATE)
    if stop <= max(0, liquidation):
        return None
    net_cost = entry * (1 + settings.fee_rate)
    unit_risk = net_cost - stop * (1 - settings.fee_rate)
    target = (net_cost + settings.reward_to_risk * unit_risk) / (1 - settings.fee_rate)
    return Bracket(stop, target, liquidation, unit_risk * fill.contracts * INR_PER_POINT_PER_LOT)


def net_pnl(entry: Fill, exit_: Fill, fee_rate: Decimal) -> Decimal:
    return exit_.notional_inr * (1 - fee_rate) - entry.notional_inr * (1 + fee_rate)
