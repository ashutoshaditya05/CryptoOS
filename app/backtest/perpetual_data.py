import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from app.market.delta import DELTA_URL, MAX_CANDLES, DeltaCandleProvider
from app.market.models import BITCOIN, Candle, Timeframe, candle_window, floor_time

SYMBOL = "BTCUSD"
FRAME = Timeframe.THREE_MINUTES
FUNDING_HOURS_UTC = (0, 8, 16)
SERIES = {
    "trade": (SYMBOL, FRAME),
    "mark": (f"MARK:{SYMBOL}", FRAME),
    "funding": (f"FUNDING:{SYMBOL}", Timeframe.ONE_HOUR),
}


@dataclass(frozen=True)
class PerpetualData:
    start: datetime
    end: datetime
    trades: dict[datetime, Candle]
    marks: dict[datetime, Candle]
    funding: dict[datetime, Decimal]
    sha256: str


def download(start: datetime, end: datetime, path: Path) -> None:
    start, end = candle_window(start, end, FRAME)
    if end > floor_time(datetime.now(UTC), FRAME):
        raise ValueError("The backtest end must be on or before the latest closed candle.")
    payload = {"start": start.isoformat(), "end": end.isoformat(), "series": {}}
    with httpx.Client(base_url=DELTA_URL, timeout=20) as client:
        provider = DeltaCandleProvider(client)
        for name, (symbol, frame) in SERIES.items():
            first = floor_time(start - timedelta(days=1), frame)
            last = floor_time(end - FRAME.duration, frame) + frame.duration
            chunk_size = MAX_CANDLES * frame.duration
            rows = []
            while first < last:
                chunk_end = min(first + chunk_size, last)
                rows.extend(provider.history(symbol, frame, first, chunk_end))
                first = chunk_end
            payload["series"][name] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")


def load(path: Path) -> PerpetualData:
    raw = path.read_bytes()
    payload = json.loads(raw, parse_float=Decimal)
    start, end = candle_window(
        datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"]), FRAME
    )

    def candles(name: str) -> dict[datetime, Candle]:
        symbol, frame = SERIES[name]
        bars = {}
        for row in payload["series"][name]:
            timestamp = datetime.fromtimestamp(row["time"], UTC)
            candle = Candle(
                venue=BITCOIN.venue,
                symbol=symbol,
                timeframe=frame,
                open_time=timestamp,
                close_time=timestamp + frame.duration,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row.get("volume"),
                is_closed=True,
            )
            if timestamp in bars and bars[timestamp] != candle:
                raise ValueError(f"Conflicting {symbol} candles at {timestamp.isoformat()}.")
            bars[timestamp] = candle
        return bars

    funding = {}
    for row in payload["series"]["funding"]:
        timestamp = datetime.fromtimestamp(row["time"], UTC)
        if timestamp.hour not in FUNDING_HOURS_UTC or timestamp.minute != 0:
            continue
        rate = Decimal(row["open"]) / 100
        if not rate.is_finite() or abs(rate) > Decimal("0.05"):
            raise ValueError(f"Invalid funding rate at {timestamp.isoformat()}.")
        if timestamp in funding and funding[timestamp] != rate:
            raise ValueError(f"Conflicting funding rates at {timestamp.isoformat()}.")
        funding[timestamp] = rate

    return PerpetualData(
        start, end, candles("trade"), candles("mark"), funding, hashlib.sha256(raw).hexdigest()
    )
