from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from app.backtest.fills import bracket, buy, net_pnl, sweep
from app.backtest.models import (
    FUNDING_INTERVAL_US,
    INR_PER_POINT_PER_LOT,
    MAX_MARK_AGE_US,
    SECOND_US,
    Bracket,
    Fill,
    ReplaySettings,
)
from app.market.book_tape import Frame
from app.strategies.orderbook_imbalance import Signal


@dataclass(frozen=True, slots=True)
class Trade:
    signal_us: int
    entry_us: int
    exit_us: int
    entry: Fill
    exit: Fill | None
    bracket: Bracket
    reason: str
    pnl_inr: Decimal


def frame_at(times: list[int], start: int, target: int, settings: ReplaySettings) -> int | None:
    index = bisect_left(times, target, lo=start)
    if index == len(times) or times[index] - target > settings.max_quote_age_us:
        return None
    return index


def entry_at(
    frames: list[Frame], times: list[int], signal: Signal, settings: ReplaySettings
) -> tuple[int, Fill] | str:
    source = frames[signal.index]
    index = frame_at(times, signal.index + 1, source.time_us + settings.latency_us, settings)
    if index is None:
        return "entry_unobserved"
    current = frames[index]
    quote = current.quote
    if (
        quote is None
        or current.segment != source.segment
        or not current.has_quote(settings.max_quote_age_us)
    ):
        return "entry_book_gap"
    if quote.spread_bps > settings.max_spread_bps:
        return "entry_wide_spread"
    fill = buy(quote, settings)
    return (index, fill) if fill is not None else "entry_insufficient_depth"


def _fresh_mark(frame: Frame) -> bool:
    return frame.mark is not None and frame.time_us - frame.mark_us <= MAX_MARK_AGE_US


def simulate(
    frames: list[Frame], times: list[int], signals: list[Signal], settings: ReplaySettings
) -> tuple[dict, list[Trade]]:
    cash = peak = settings.initial_cash_inr
    drawdown = Decimal(0)
    trades: list[Trade] = []
    skipped: Counter[str] = Counter()
    censor = None
    free_index = -1
    max_hold_us = settings.max_hold_seconds * SECOND_US
    fee = settings.fee_rate

    for signal in signals:
        if signal.index <= free_index:
            skipped["position_already_open"] += 1
            continue
        entry = entry_at(frames, times, signal, settings)
        if isinstance(entry, str):
            skipped[entry] += 1
            continue
        entry_index, fill = entry
        opened = frames[entry_index]
        quote = opened.quote
        assert quote is not None
        plan = bracket(fill, settings)
        if plan is None or quote.bid <= plan.stop_usd:
            skipped["invalid_bracket"] += 1
            continue
        if not _fresh_mark(opened):
            skipped["missing_entry_mark"] += 1
            continue
        funding_period = opened.time_us // FUNDING_INTERVAL_US
        hold_until = opened.time_us + max_hold_us
        if (hold_until + settings.latency_us) // FUNDING_INTERVAL_US != funding_period:
            skipped["funding_boundary"] += 1
            continue
        margin = fill.notional_inr / settings.leverage
        entry_fee = fill.notional_inr * fee
        exit_reserve = plan.target_usd * fill.contracts * INR_PER_POINT_PER_LOT * fee
        if cash < margin + entry_fee + exit_reserve:
            skipped["insufficient_margin_and_fees"] += 1
            continue

        exit_reason = None
        exit_due = 0
        for index in range(entry_index, len(frames)):
            frame = frames[index]
            quote, mark = frame.quote, frame.mark
            if frame.segment != opened.segment or not frame.has_quote(settings.max_quote_age_us):
                censor = "book_gap"
                break
            if frame.time_us // FUNDING_INTERVAL_US != funding_period:
                censor = "funding_boundary_during_exit"
                break
            if not _fresh_mark(frame):
                censor = "stale_mark"
                break
            assert quote is not None and mark is not None

            exit_fill = None
            if mark <= plan.liquidation_usd:
                reason = "LIQUIDATION"
                pnl = -margin - entry_fee
            elif exit_reason is not None and frame.time_us >= exit_due:
                if frame.time_us - exit_due > settings.max_quote_age_us:
                    censor = "exit_quote_unobserved"
                    break
                exit_fill = sweep(quote.bids, fill.contracts)
                if exit_fill is None:
                    censor = "insufficient_exit_depth"
                    break
                reason = exit_reason
                pnl = net_pnl(fill, exit_fill, fee)
            else:
                equity = cash + (mark - fill.average_usd) * fill.contracts * INR_PER_POINT_PER_LOT
                equity -= entry_fee
                peak = max(peak, equity)
                drawdown = max(drawdown, (peak - equity) / peak)
                if exit_reason is None:
                    if quote.bid <= plan.stop_usd:
                        exit_reason = "STOP_LOSS"
                    elif quote.bid >= plan.target_usd:
                        executable = sweep(quote.bids, fill.contracts)
                        if executable is not None and executable.average_usd >= plan.target_usd:
                            exit_reason = "TAKE_PROFIT"
                    if exit_reason is None and frame.time_us >= hold_until:
                        exit_reason = "TIME_EXIT"
                    if exit_reason is not None:
                        exit_due = frame.time_us + settings.latency_us
                continue

            cash += pnl
            peak = max(peak, cash)
            drawdown = max(drawdown, (peak - cash) / peak)
            trades.append(
                Trade(
                    times[signal.index],
                    opened.time_us,
                    frame.time_us,
                    fill,
                    exit_fill,
                    plan,
                    reason,
                    pnl,
                )
            )
            free_index = index
            break
        else:
            censor = "capture_end"
        if censor is not None:
            censor = {"reason": censor, "entry_us": opened.time_us, "entry": fill, "bracket": plan}
            break

    realized = cash - settings.initial_cash_inr
    wins = [trade.pnl_inr for trade in trades if trade.pnl_inr > 0]
    losses = [-trade.pnl_inr for trade in trades if trade.pnl_inr < 0]
    return {
        "signals": len(signals),
        "closed_trades": len(trades),
        "realized_pnl_inr": realized,
        "closed_trade_expectancy_inr": realized / len(trades) if trades else None,
        "win_rate": Decimal(len(wins)) / len(trades) if trades else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "mark_drawdown": drawdown,
        "largest_closed_loss_inr": max(losses, default=None),
        "exits": dict(Counter(trade.reason for trade in trades)),
        "skipped": dict(skipped),
        "censored_position": censor,
        "unprocessed_signals": len(signals) - len(trades) - sum(skipped.values()) - bool(censor),
    }, trades
