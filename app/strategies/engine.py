import hashlib
import json
from datetime import UTC, datetime, timedelta

from app.market.models import BITCOIN, Candle, candle_window, floor_time
from app.market.storage import CandleStore
from app.strategies.models import Position, PullbackSettings, Signal
from app.strategies.registry import DEFAULT_STRATEGY_VERSION, get_strategy

MAX_SIGNAL_SLOTS = 2_000


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


def generate_signals(
    store: CandleStore,
    start: datetime,
    end: datetime,
    *,
    settings: PullbackSettings,
    position: Position = Position.FLAT,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    now: datetime | None = None,
) -> list[Signal]:
    strategy = get_strategy(strategy_version)
    signal_frame, context_frame = strategy.signal_timeframe, strategy.context_timeframe
    signal_step, context_step = signal_frame.duration, context_frame.duration
    start, end = candle_window(start, end, signal_frame)
    if (end - start) // signal_step > MAX_SIGNAL_SLOTS:
        raise ValueError(f"Limit each query to {MAX_SIGNAL_SLOTS} {signal_frame} candle slots.")

    end = min(end, floor_time(now if now is not None else datetime.now(UTC), signal_frame))
    if end <= start:
        return []
    try:
        signal_start = start - (strategy.signal_warmup - 1) * signal_step
        context_start = floor_time(start, context_frame) - strategy.context_warmup * context_step
        context_end = floor_time(end, context_frame)
    except OverflowError as exc:
        raise ValueError("The date range must leave room for strategy warm-up.") from exc
    if end > datetime.max.replace(tzinfo=UTC) - signal_step:
        raise ValueError("The end must leave room for signal expiry.")

    signal_bars, context_bars = store.read_windows(
        BITCOIN,
        [(signal_frame, signal_start, end), (context_frame, context_start, context_end)],
    )
    signals_by_close = {bar.close_time: bar for bar in signal_bars if bar.is_closed}
    context_by_close = {bar.close_time: bar for bar in context_bars if bar.is_closed}
    settings_fingerprint = settings.fingerprint()
    results = []
    for timestamp in signals_by_close:
        if not start < timestamp <= end:
            continue

        signal_history = _recent_bars(
            signals_by_close, timestamp, signal_step, strategy.signal_warmup
        )
        context_history = _recent_bars(
            context_by_close,
            floor_time(timestamp, context_frame),
            context_step,
            strategy.context_warmup,
        )
        decision = strategy.evaluate(signal_history, context_history, position, settings)
        correlation_id = hashlib.sha256(
            f"{BITCOIN.venue}/{BITCOIN.symbol}/{signal_frame}/{timestamp.isoformat()}".encode()
        ).hexdigest()
        identity = (
            correlation_id,
            strategy.version,
            settings_fingerprint,
            position,
            decision.model_dump(mode="json"),
        )
        signal_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        results.append(
            Signal(
                **decision.model_dump(),
                signal_id=signal_id,
                correlation_id=correlation_id,
                strategy_version=strategy.version,
                settings_fingerprint=settings_fingerprint,
                venue=BITCOIN.venue,
                symbol=BITCOIN.symbol,
                timeframe=signal_frame,
                position=position,
                timestamp=timestamp,
                valid_until=timestamp + signal_step,
            )
        )
    return results
