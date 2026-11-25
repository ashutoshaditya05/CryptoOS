import argparse
import json
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.backtest.fills import BASIS_POINTS, ZERO, round_down, round_up
from app.backtest.perpetual_data import (
    FRAME,
    FUNDING_HOURS_UTC,
    SYMBOL,
    PerpetualData,
    download,
    load,
)
from app.market.delta import DeltaError
from app.market.models import Candle
from app.strategies.models import Position
from app.strategies.vwap_ema_cross import VwapEmaCross

USD_INR = Decimal(85)
LOT_BTC = Decimal("0.001")
TICK_USD = Decimal("0.5")
MAINTENANCE_RATE = Decimal("0.0025")
STRATEGY_VERSION = "vwap_ema_btcusd_bracket@1.0.0"
type Positive = Annotated[Decimal, Field(gt=0)]


class PerpetualSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    initial_cash_inr: Positive = Decimal(100_000)
    margin_inr: Positive = Decimal(50_000)
    leverage: Annotated[int, Field(ge=1, le=200)] = 25
    stop_points: Positive = Decimal(200)
    reward_to_risk: Positive = Decimal(2)
    fee_bps: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("5.9")
    slippage_bps: Annotated[Decimal, Field(ge=0, le=100)] = Decimal(2)

    @model_validator(mode="after")
    def validate_margin(self) -> "PerpetualSettings":
        if self.margin_inr >= self.initial_cash_inr:
            raise ValueError("Per-trade margin must leave cash for fees.")
        if self.margin_inr * self.leverage > 100_000 * USD_INR:
            raise ValueError("This model covers the base BTCUSD margin tier up to $100,000.")
        return self

    @property
    def fee_rate(self) -> Decimal:
        return self.fee_bps / BASIS_POINTS

    @property
    def slippage(self) -> Decimal:
        return self.slippage_bps / BASIS_POINTS


@dataclass
class Holding:
    opened_at: datetime
    quantity_btc: Decimal
    entry_usd: Decimal
    stop_usd: Decimal
    target_usd: Decimal
    margin_inr: Decimal
    entry_fee_inr: Decimal
    planned_loss_inr: Decimal
    funding_inr: Decimal = ZERO

    @property
    def inr_per_point(self) -> Decimal:
        return self.quantity_btc * USD_INR

    @property
    def collateral_inr(self) -> Decimal:
        return self.margin_inr - self.funding_inr

    @property
    def liquidation_usd(self) -> Decimal:
        return (self.entry_usd - self.collateral_inr / self.inr_per_point) / (1 - MAINTENANCE_RATE)


@dataclass(frozen=True)
class ClosedTrade:
    plan: Holding
    closed_at: datetime
    reason: str
    exit_usd: Decimal
    exit_fee_inr: Decimal
    pnl_inr: Decimal


def sell_fill(reference: Decimal, settings: PerpetualSettings) -> Decimal:
    return round_down(reference * (1 - settings.slippage), TICK_USD)


def plan_trade(
    candle: Candle, previous: Candle, cash: Decimal, settings: PerpetualSettings
) -> Holding | None:
    entry = round_up(candle.open * (1 + settings.slippage), TICK_USD)
    stop = round_up(entry - settings.stop_points, TICK_USD)
    if stop <= 0 or candle.open <= stop:
        return None

    price_inr = entry * USD_INR
    intended = settings.margin_inr * settings.leverage / price_inr
    # Delta's BTCUSD volume is in contracts, each for 0.001 BTC.
    available = (previous.volume or ZERO) * LOT_BTC * Decimal("0.1")
    quantity = round_down(min(intended, available), LOT_BTC)
    if quantity == 0:
        return None
    fee = settings.fee_rate
    entry_fee = quantity * price_inr * fee
    margin = quantity * price_inr / settings.leverage
    unit_cost = entry * (1 + fee)
    stop_fill = sell_fill(stop, settings)
    risk_usd = unit_cost - stop_fill * (1 - fee)
    required_sell = round_up((unit_cost + settings.reward_to_risk * risk_usd) / (1 - fee), TICK_USD)
    target = round_up(required_sell / (1 - settings.slippage), TICK_USD)
    exit_fee_reserve = quantity * USD_INR * required_sell * fee
    if cash < margin + entry_fee + exit_fee_reserve:
        return None
    holding = Holding(
        candle.open_time,
        quantity,
        entry,
        stop,
        target,
        margin,
        entry_fee,
        risk_usd * quantity * USD_INR,
    )
    if stop_fill <= holding.liquidation_usd:
        return None
    return holding


def entries_for(data: PerpetualData) -> set[datetime]:
    strategy = VwapEmaCross()
    history = deque(maxlen=strategy.signal_warmup)
    entries = set()
    for timestamp, candle in sorted(data.trades.items()):
        if timestamp >= data.end:
            break
        if history and history[-1].close_time != timestamp:
            history.clear()
        history.append(candle)
        if timestamp >= data.start:
            decision = strategy.evaluate(list(history), (), Position.FLAT)
            if decision.action == "BUY":
                entries.add(candle.close_time)
    return entries


def exit_trigger(
    candle: Candle, mark: Candle, holding: Holding
) -> tuple[str, Decimal, datetime] | None:
    liquidation = holding.liquidation_usd
    if mark.open <= liquidation:
        return "LIQUIDATION", mark.open, candle.open_time
    if candle.open <= holding.stop_usd:
        return "STOP_LOSS", candle.open, candle.open_time
    if candle.open >= holding.target_usd:
        return "TAKE_PROFIT", holding.target_usd, candle.open_time
    # The ordering of intrabar extremes is unknown. Resolve ties conservatively.
    if mark.low <= liquidation:
        return "LIQUIDATION", liquidation, candle.close_time
    if candle.low <= holding.stop_usd:
        return "STOP_LOSS", holding.stop_usd, candle.close_time
    if candle.high >= holding.target_usd:
        return "TAKE_PROFIT", holding.target_usd, candle.close_time
    return None


def close_trade(
    holding: Holding, trigger: tuple[str, Decimal, datetime], settings: PerpetualSettings
) -> ClosedTrade:
    reason, reference, timestamp = trigger
    if reason == "LIQUIDATION":
        # Model the isolated collateral as consumed, including the liquidation charge.
        exit_price = holding.entry_usd - holding.collateral_inr / holding.inr_per_point
        net = -holding.margin_inr - holding.entry_fee_inr
        return ClosedTrade(holding, timestamp, reason, exit_price, ZERO, net)
    exit_price = sell_fill(reference, settings)
    exit_fee = exit_price * holding.inr_per_point * settings.fee_rate
    net = (exit_price - holding.entry_usd) * holding.inr_per_point
    net -= holding.entry_fee_inr + exit_fee + holding.funding_inr
    return ClosedTrade(holding, timestamp, reason, exit_price, exit_fee, net)


def simulate(
    data: PerpetualData,
    entries: set[datetime],
    settings: PerpetualSettings,
    start: datetime,
    end: datetime,
) -> tuple[dict, list[ClosedTrade]]:
    cash = peak = settings.initial_cash_inr
    drawdown = ZERO
    trades: list[ClosedTrade] = []
    holding = None
    pending = False
    missing = skipped = 0
    step = FRAME.duration
    timestamp = start
    while timestamp < end:
        candle = data.trades.get(timestamp)
        mark = data.marks.get(timestamp)
        if candle is None or mark is None:
            if holding is not None:
                raise ValueError(f"Missing trade or mark data during a position at {timestamp}.")
            missing += 1
            pending = False
            timestamp += step
            continue

        if holding is not None and timestamp.minute == 0 and timestamp.hour in FUNDING_HOURS_UTC:
            rate = data.funding.get(timestamp)
            if rate is None:
                raise ValueError(f"Missing funding during a position at {timestamp}.")
            funding = rate * mark.open * holding.inr_per_point
            holding.funding_inr += funding
            cash -= funding

        if pending:
            previous = data.trades[timestamp - step]
            holding = plan_trade(candle, previous, cash, settings)
            if holding is None:
                skipped += 1
            else:
                cash -= holding.entry_fee_inr

        if holding is not None:
            trigger = exit_trigger(candle, mark, holding)
            if trigger is None and candle.close_time == end:
                trigger = ("WINDOW_END", candle.close, candle.close_time)
            if trigger is not None:
                trade = close_trade(holding, trigger, settings)
                trades.append(trade)
                cash += trade.pnl_inr + holding.entry_fee_inr + holding.funding_inr
                holding = None

        equity = cash
        if holding is not None:
            equity += (mark.close - holding.entry_usd) * holding.inr_per_point
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        pending = holding is None and candle.close_time in entries
        timestamp += step

    wins = [trade.pnl_inr for trade in trades if trade.pnl_inr > 0]
    losses = [-trade.pnl_inr for trade in trades if trade.pnl_inr < 0]
    returns = cash - settings.initial_cash_inr
    return {
        "start": start,
        "end": end,
        "missing_trade_or_mark_bars": missing,
        "skipped_entries": skipped,
        "closed_trades": len(trades),
        "win_rate": Decimal(len(wins)) / len(trades) if trades else None,
        "net_pnl_inr": returns,
        "return_fraction": returns / settings.initial_cash_inr,
        "net_expectancy_inr": returns / len(trades) if trades else None,
        "profit_factor": sum(wins, ZERO) / sum(losses, ZERO) if losses else None,
        "max_drawdown": drawdown,
        "max_trade_loss_inr": max(losses, default=ZERO),
        "fees_inr": sum((trade.plan.entry_fee_inr + trade.exit_fee_inr for trade in trades), ZERO),
        "funding_paid_inr": sum((trade.plan.funding_inr for trade in trades), ZERO),
        "exits": dict(Counter(trade.reason for trade in trades)),
    }, trades


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the BTCUSD perpetual VWAP/EMA strategy.")
    parser.add_argument("--data", type=Path, required=True, help="Futures dataset JSON file")
    parser.add_argument("--start", help="Download start, with a UTC offset")
    parser.add_argument("--end", help="Download end, with a UTC offset")
    parser.add_argument("--leverage", type=int, default=25)
    parser.add_argument("--margin-inr", default="50000")
    parser.add_argument("--stop-points", default="200")
    parser.add_argument("--reward-to-risk", default="2")
    parser.add_argument("--slippage-bps", default="2")
    parser.add_argument(
        "--trades", action="store_true", help="Include the full-period trade ledger"
    )
    args = parser.parse_args()
    if bool(args.start) != bool(args.end):
        parser.error("Provide both --start and --end to download fresh data.")
    try:
        settings = PerpetualSettings.model_validate(
            {
                name: getattr(args, name)
                for name in (
                    "leverage",
                    "margin_inr",
                    "stop_points",
                    "reward_to_risk",
                    "slippage_bps",
                )
            }
        )
        if args.start and args.end:
            download(
                datetime.fromisoformat(args.start), datetime.fromisoformat(args.end), args.data
            )
        data = load(args.data)
        entries = entries_for(data)
        summary, trades = simulate(data, entries, settings, data.start, data.end)
        slots = (data.end - data.start) // FRAME.duration
        split = data.start + (slots * 4 // 5) * FRAME.duration
        report = {
            "symbol": SYMBOL,
            "strategy_version": STRATEGY_VERSION,
            "settings": settings.model_dump(mode="json"),
            "data_sha256": data.sha256,
            "full_period": summary,
        }
        if data.start < split < data.end:
            report["first_80_pct"] = simulate(data, entries, settings, data.start, split)[0]
            report["last_20_pct"] = simulate(data, entries, settings, split, data.end)[0]
        if args.trades:
            report["trades"] = [asdict(trade) for trade in trades]
        print(json.dumps(report, default=str, indent=2))
    except (OSError, ValueError, DeltaError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
