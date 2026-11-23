import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Self

from app.market.models import BITCOIN, Candle, Timeframe, candle_window, floor_time
from app.market.storage import CandleStore
from app.strategies.ema_pullback import EmaPullback
from app.strategies.models import Decision, Position, PullbackSettings, Signal
from app.strategies.registry import DEFAULT_STRATEGY_VERSION, get_strategy
from app.strategies.vwap_ema_cross import VwapEmaCross

MAX_SIGNAL_SLOTS = 2_000
type Evaluate = Callable[[Sequence[Candle], Sequence[Candle], Position], Decision]


def _recent_bars(
    bars_by_close: dict[datetime, Candle], end: datetime, step: timedelta, limit: int
) -> list[Candle]:
    bars = []
    for distance in range(limit):
        bar = bars_by_close.get(end - step * distance)
        if bar is None:
            break
        bars.append(bar)
    bars.reverse()
    return bars


def _configure_strategy(
    strategy: EmaPullback | VwapEmaCross, settings: PullbackSettings | None
) -> tuple[Evaluate, str]:
    if isinstance(strategy, EmaPullback):
        settings = settings if settings is not None else PullbackSettings()
        return partial(strategy.evaluate, settings=settings), settings.fingerprint()
    if settings is not None and settings.fingerprint() != PullbackSettings().fingerprint():
        raise ValueError("The EMA pullback settings apply only to the EMA pullback strategy.")
    return strategy.evaluate, strategy.settings_fingerprint


@dataclass(frozen=True)
class Replay:
    start: datetime
    end: datetime
    strategy: EmaPullback | VwapEmaCross
    candles: tuple[Candle, ...]
    signal_bars: dict[datetime, Candle]
    context_bars: dict[datetime, Candle]
    evaluate: Evaluate
    settings_fingerprint: str

    @property
    def timeframe(self) -> Timeframe:
        return self.strategy.signal_timeframe

    def with_settings(self, settings: PullbackSettings | None) -> Self:
        evaluate, fingerprint = _configure_strategy(self.strategy, settings)
        return replace(self, evaluate=evaluate, settings_fingerprint=fingerprint)

    def signal_at(self, timestamp: datetime, position: Position) -> Signal:
        step = self.timeframe.duration
        signal_history = _recent_bars(
            self.signal_bars, timestamp, step, self.strategy.signal_warmup
        )
        context_history = []
        context_frame = self.strategy.context_timeframe
        if context_frame is not None:
            context_history = _recent_bars(
                self.context_bars,
                floor_time(timestamp, context_frame),
                context_frame.duration,
                self.strategy.context_warmup,
            )
        decision = self.evaluate(signal_history, context_history, position)
        correlation_id = hashlib.sha256(
            f"{BITCOIN.venue}/{BITCOIN.symbol}/{self.timeframe}/{timestamp.isoformat()}".encode()
        ).hexdigest()
        identity = (
            correlation_id,
            self.strategy.version,
            self.settings_fingerprint,
            position,
            decision.model_dump(mode="json"),
        )
        signal_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return Signal(
            **decision.model_dump(),
            signal_id=signal_id,
            correlation_id=correlation_id,
            strategy_version=self.strategy.version,
            settings_fingerprint=self.settings_fingerprint,
            venue=BITCOIN.venue,
            symbol=BITCOIN.symbol,
            timeframe=self.timeframe,
            position=position,
            timestamp=timestamp,
            valid_until=timestamp + step,
        )


def prepare_replay(
    store: CandleStore,
    start: datetime,
    end: datetime,
    *,
    settings: PullbackSettings | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    max_slots: int = MAX_SIGNAL_SLOTS,
    now: datetime | None = None,
) -> Replay:
    strategy = get_strategy(strategy_version)
    evaluate, settings_fingerprint = _configure_strategy(strategy, settings)
    signal_frame, context_frame = strategy.signal_timeframe, strategy.context_timeframe
    signal_step = signal_frame.duration
    start, end = candle_window(start, end, signal_frame)
    if (end - start) // signal_step > max_slots:
        raise ValueError(f"Limit each query to {max_slots} {signal_frame} candle slots.")

    end = min(end, floor_time(now if now is not None else datetime.now(UTC), signal_frame))
    if end <= start:
        return Replay(start, end, strategy, (), {}, {}, evaluate, settings_fingerprint)
    try:
        signal_start = start - (strategy.signal_warmup - 1) * signal_step
        windows = [(signal_frame, signal_start, end)]
        if context_frame is not None:
            context_start = floor_time(start, context_frame)
            context_start -= strategy.context_warmup * context_frame.duration
            windows.append((context_frame, context_start, floor_time(end, context_frame)))
    except OverflowError as exc:
        raise ValueError("The date range must leave room for strategy warm-up.") from exc
    if end > datetime.max.replace(tzinfo=UTC) - signal_step:
        raise ValueError("The end must leave room for signal expiry.")

    signal_bars, *context = store.read_windows(BITCOIN, windows)
    signals_by_close = {bar.close_time: bar for bar in signal_bars if bar.is_closed}
    context_by_close = {bar.close_time: bar for bars in context for bar in bars if bar.is_closed}
    candles = tuple(bar for ts, bar in signals_by_close.items() if start < ts <= end)
    return Replay(
        start,
        end,
        strategy,
        candles,
        signals_by_close,
        context_by_close,
        evaluate,
        settings_fingerprint,
    )


def generate_signals(
    store: CandleStore,
    start: datetime,
    end: datetime,
    *,
    settings: PullbackSettings | None = None,
    position: Position = Position.FLAT,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    now: datetime | None = None,
) -> list[Signal]:
    replay = prepare_replay(
        store, start, end, settings=settings, strategy_version=strategy_version, now=now
    )
    return [replay.signal_at(candle.close_time, position) for candle in replay.candles]
