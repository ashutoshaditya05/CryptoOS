from collections.abc import Sequence
from decimal import Decimal, localcontext

from app.market.indicators import INDICATOR_CONTEXT, atr_adx, ema
from app.market.models import DECIMAL_PRECISION, DECIMAL_SCALE, Candle, Timeframe
from app.strategies.models import Decision, Position, PullbackSettings, Regime

PRICE_INCREMENT = Decimal(f"1e-{DECIMAL_SCALE}")
MAX_PRICE = Decimal(f"1e{DECIMAL_PRECISION - DECIMAL_SCALE}")


class EmaPullback:
    version = "ema_pullback@1.0.0"
    signal_timeframe = Timeframe.FIFTEEN_MINUTES
    context_timeframe = Timeframe.ONE_HOUR
    signal_warmup = 150
    context_warmup = 600

    def evaluate(
        self,
        signal_bars: Sequence[Candle],
        hourly_bars: Sequence[Candle],
        position: Position,
        settings: PullbackSettings,
    ) -> Decision:
        hourly_closes = [bar.close for bar in hourly_bars]
        signal_closes = [bar.close for bar in signal_bars]
        hourly_fast, hourly_slow = ema(hourly_closes, 50), ema(hourly_closes, 200)
        signal_fast, signal_slow = ema(signal_closes, 20), ema(signal_closes, 50)
        if hourly_fast is None or hourly_slow is None or signal_fast is None or signal_slow is None:
            reasons = []
            if len(hourly_bars) < 200:
                reasons.append("INSUFFICIENT_HOURLY_HISTORY")
            if len(signal_bars) < 50:
                reasons.append("INSUFFICIENT_SIGNAL_HISTORY")
            return Decision(
                action="HOLD", regime=Regime.DATA_UNAVAILABLE, reason_codes=tuple(reasons)
            )

        atr, adx = atr_adx(signal_bars, 14)
        assert atr is not None and adx is not None  # 50 bars cover both 14-period indicators.
        current = signal_bars[-1]
        hourly_up, signal_up = hourly_fast > hourly_slow, signal_fast > signal_slow

        with localcontext(INDICATOR_CONTEXT):
            if atr / current.close > settings.max_atr_fraction:
                regime = Regime.HIGH_VOLATILITY
            elif adx < settings.min_adx or (
                hourly_fast == hourly_slow and signal_fast == signal_slow
            ):
                regime = Regime.SIDEWAYS
            elif hourly_up and signal_up:
                regime = Regime.UPTREND
            elif hourly_fast < hourly_slow and signal_fast < signal_slow:
                regime = Regime.DOWNTREND
            else:
                regime = Regime.MIXED_TREND

            if position == Position.LONG:
                if not hourly_up or not signal_up:
                    return Decision(
                        action="SELL", regime=regime, reason_codes=("TREND_INVALIDATED",)
                    )
                return Decision(
                    action="HOLD", regime=regime, reason_codes=("POSITION_ALREADY_LONG",)
                )
            if regime != Regime.UPTREND:
                return Decision(action="HOLD", regime=regime, reason_codes=(regime.value,))
            if not current.low <= signal_fast < current.close:
                return Decision(action="HOLD", regime=regime, reason_codes=("NO_EMA20_RECLAIM",))

            entry = current.close.quantize(PRICE_INCREMENT)
            stop = (entry - atr * settings.atr_stop_multiple).quantize(PRICE_INCREMENT)
            target = (entry + (entry - stop) * settings.reward_risk_ratio).quantize(PRICE_INCREMENT)
            if not 0 < stop < entry < target < MAX_PRICE:
                return Decision(action="HOLD", regime=regime, reason_codes=("INVALID_ATR_LEVELS",))
            return Decision(
                action="BUY",
                regime=regime,
                reason_codes=("EMA20_RECLAIM", "ATR_STOP_AND_TARGET"),
                entry_reference=entry,
                stop_loss=stop,
                take_profit=target,
            )
