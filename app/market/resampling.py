from datetime import datetime
from decimal import Decimal, localcontext
from itertools import groupby

from app.market.models import DECIMAL_PRECISION, Candle, Market, Timeframe, floor_time
from app.market.storage import CandleStore


def resampling_ratio(source: Timeframe, target: Timeframe) -> int:
    ratio, remainder = divmod(target.duration, source.duration)
    if ratio < 2 or remainder:
        raise ValueError(
            "Target timeframe must be a larger whole multiple of the source timeframe."
        )
    return ratio


def fetch_resampled(
    store: CandleStore,
    market: Market,
    target: Timeframe,
    start: datetime,
    end: datetime,
    *,
    source: Timeframe,
) -> list[Candle]:
    ratio = resampling_ratio(source, target)
    source_candles = store.read(market, source, start, end)
    candles = []
    for open_time, group in groupby(source_candles, lambda bar: floor_time(bar.open_time, target)):
        bars = list(group)
        if len(bars) != ratio or any(
            not bar.is_closed or bar.open_time != open_time + index * source.duration
            for index, bar in enumerate(bars)
        ):
            continue

        volumes = [bar.volume for bar in bars if bar.volume is not None]
        with localcontext(prec=DECIMAL_PRECISION + 4):
            volume = sum(volumes, start=Decimal(0)) if len(volumes) == ratio else None
        candles.append(
            Candle(
                venue=market.venue,
                symbol=market.symbol,
                timeframe=target,
                open_time=open_time,
                close_time=open_time + target.duration,
                open=bars[0].open,
                high=max(bar.high for bar in bars),
                low=min(bar.low for bar in bars),
                close=bars[-1].close,
                volume=volume,
                is_closed=True,
            )
        )
    return candles
