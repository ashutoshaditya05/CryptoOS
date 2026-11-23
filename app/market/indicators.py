from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise

from app.market.models import DECIMAL_PRECISION, Candle

INDICATOR_CONTEXT = Context(prec=DECIMAL_PRECISION + 8, rounding=ROUND_HALF_EVEN)
ZERO = Decimal(0)


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("EMA period must be positive.")
    if len(values) < period:
        return None

    with localcontext(INDICATOR_CONTEXT):
        average = sum(values[:period], ZERO) / period
        weight = Decimal(2) / (period + 1)
        for value in values[period:]:
            average += (value - average) * weight
        return average


def _wilder_averages(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        return []
    average = sum(values[:period], ZERO) / period
    results = [average]
    for value in values[period:]:
        average = (average * (period - 1) + value) / period
        results.append(average)
    return results


def atr_adx(candles: Sequence[Candle], period: int) -> tuple[Decimal | None, Decimal | None]:
    if period < 1:
        raise ValueError("ATR and ADX periods must be positive.")

    with localcontext(INDICATOR_CONTEXT):
        ranges, positive, negative = [], [], []
        for previous, current in pairwise(candles):
            ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
            up = current.high - previous.high
            down = previous.low - current.low
            positive.append(up if up > max(down, ZERO) else ZERO)
            negative.append(down if down > max(up, ZERO) else ZERO)

        atr = _wilder_averages(ranges, period)
        plus_dm = _wilder_averages(positive, period)
        minus_dm = _wilder_averages(negative, period)
        # True range cancels in the ratio of directional-index difference to sum.
        dx = [
            100 * abs(plus - minus) / (plus + minus) if plus + minus else ZERO
            for plus, minus in zip(plus_dm, minus_dm, strict=True)
        ]
        adx = _wilder_averages(dx, period)
        return (atr[-1] if atr else None, adx[-1] if adx else None)
