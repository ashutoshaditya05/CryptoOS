from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal, localcontext
from hashlib import sha256

from app.market.indicators import INDICATOR_CONTEXT, ema
from app.market.models import Candle, Timeframe
from app.strategies.models import Decision, Position, Regime

EMA_PERIOD = 9


class VwapEmaCross:
    version = "vwap_ema_cross@1.0.0"
    signal_timeframe = Timeframe.THREE_MINUTES
    signal_warmup = timedelta(days=1) // signal_timeframe.duration
    context_timeframe = None
    context_warmup = 0
    settings_fingerprint = sha256(
        f"ema_period={EMA_PERIOD}|vwap_anchor=00:00Z|vwap_price=hlc3".encode()
    ).hexdigest()

    def evaluate(
        self, signal_bars: Sequence[Candle], _context_bars: Sequence[Candle], position: Position
    ) -> Decision:
        average = ema([candle.close for candle in signal_bars], EMA_PERIOD)
        if average is None:
            return Decision(
                action="HOLD",
                regime=Regime.DATA_UNAVAILABLE,
                reason_codes=("INSUFFICIENT_EMA9_HISTORY",),
            )

        current = signal_bars[-1]
        if position == Position.LONG:
            if current.close < average:
                return Decision(
                    action="SELL", regime=Regime.DOWNTREND, reason_codes=("CLOSE_BELOW_EMA9",)
                )
            return Decision(
                action="HOLD",
                regime=Regime.UPTREND if current.close > average else Regime.SIDEWAYS,
                reason_codes=("POSITION_ALREADY_LONG",),
            )

        midnight = current.open_time.replace(hour=0, minute=0, second=0, microsecond=0)
        session = [candle for candle in signal_bars if candle.open_time >= midnight]
        if session[0].open_time != midnight:
            return Decision(
                action="HOLD",
                regime=Regime.DATA_UNAVAILABLE,
                reason_codes=("INCOMPLETE_UTC_VWAP_SESSION",),
            )

        vwap = session_vwap(session)
        if vwap is None:
            return Decision(
                action="HOLD",
                regime=Regime.DATA_UNAVAILABLE,
                reason_codes=("VWAP_VOLUME_UNAVAILABLE",),
            )

        if (
            current.low < current.open
            and current.low <= vwap <= current.open <= average < current.close
        ):
            return Decision(
                action="BUY",
                regime=Regime.UPTREND,
                reason_codes=("VWAP_LOWER_WICK", "BULLISH_EMA9_CROSS"),
                entry_reference=current.close,
            )

        if current.close > max(vwap, average):
            regime = Regime.UPTREND
        elif current.close < min(vwap, average):
            regime = Regime.DOWNTREND
        else:
            regime = Regime.MIXED_TREND
        return Decision(action="HOLD", regime=regime, reason_codes=("NO_VWAP_EMA9_ENTRY",))


def session_vwap(candles: Sequence[Candle]) -> Decimal | None:
    with localcontext(INDICATOR_CONTEXT):
        total_volume = weighted_range_sum = Decimal(0)
        for candle in candles:
            if candle.volume is None:
                return None
            total_volume += candle.volume
            weighted_range_sum += (candle.high + candle.low + candle.close) * candle.volume
        return weighted_range_sum / (3 * total_volume) if total_volume else None
