from collections.abc import Sequence
from datetime import time, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import mean, stdev

from app.backtest.fills import ZERO
from app.backtest.models import EquityPoint, Metrics, Order, Trade


def percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    fractional_index = (len(ordered) - 1) * fraction
    index = int(fractional_index)
    if index + 1 >= len(ordered):
        return ordered[index]
    return ordered[index] + (fractional_index - index) * (ordered[index + 1] - ordered[index])


def metrics_for(
    equity: list[EquityPoint], orders: list[Order], trades: list[Trade], realized: Decimal
) -> Metrics:
    initial, final = equity[0], equity[-1]
    fills = [order.fill for order in orders if order.fill is not None]
    winning = [trade.net_pnl_inr for trade in trades if trade.net_pnl_inr > 0]
    losses = [trade.net_pnl_inr for trade in trades if trade.net_pnl_inr < 0]
    net = final.equity_inr - initial.equity_inr
    points = equity[1:]
    midnights = [point for point in equity if point.timestamp.time() == time.min]
    daily = [
        new.equity_inr / old.equity_inr - 1
        for old, new in pairwise(midnights)
        if new.timestamp - old.timestamp == timedelta(days=1) and old.equity_inr > 0
    ]
    annual = Decimal(365).sqrt()
    deviation = stdev(daily) if len(daily) >= 2 else None
    downside = mean([min(value, ZERO) ** 2 for value in daily]).sqrt() if len(daily) >= 2 else None

    return Metrics(
        initial_equity_inr=initial.equity_inr,
        final_equity_inr=final.equity_inr,
        net_pnl_inr=net,
        realized_pnl_inr=realized,
        unrealized_pnl_inr=net - realized,
        return_fraction=net / initial.equity_inr,
        max_drawdown=max(point.drawdown for point in equity),
        first_fill_at=fills[0].timestamp if fills else None,
        last_mark_at=final.timestamp if points else None,
        fees_inr=sum((fill.fee_inr for fill in fills), ZERO),
        spread_slippage_inr=sum((fill.spread_slippage_inr for fill in fills), ZERO),
        turnover=sum((fill.notional_inr for fill in fills), ZERO) / initial.equity_inr,
        close_exposure=Decimal(sum(point.position_btc > 0 for point in points)) / len(points)
        if points
        else ZERO,
        orders=len(orders),
        fills=len(fills),
        closed_trades=len(trades),
        win_rate=Decimal(len(winning)) / len(trades) if trades else None,
        net_expectancy_inr=mean([trade.net_pnl_inr for trade in trades]) if trades else None,
        profit_factor=sum(winning, ZERO) / -sum(losses, ZERO) if losses else None,
        daily_return_samples=len(daily),
        annualized_volatility=deviation * annual if deviation is not None else None,
        sharpe=mean(daily) / deviation * annual if deviation else None,
        sortino=mean(daily) / downside * annual if downside else None,
        daily_var_95=max(ZERO, -percentile(daily, Decimal("0.05"))) if len(daily) >= 20 else None,
    )
