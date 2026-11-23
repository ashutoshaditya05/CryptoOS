from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DECIMAL_PRECISION = 56
DECIMAL_SCALE = 36
type Price = Annotated[
    Decimal, Field(gt=0, max_digits=DECIMAL_PRECISION, decimal_places=DECIMAL_SCALE)
]
type Volume = Annotated[
    Decimal, Field(ge=0, max_digits=DECIMAL_PRECISION, decimal_places=DECIMAL_SCALE)
]


class Timeframe(StrEnum):
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    ONE_HOUR = "1h"
    FOUR_HOURS = "4h"

    @property
    def duration(self) -> timedelta:
        return DURATIONS[self]


DURATIONS = {
    Timeframe.FIVE_MINUTES: timedelta(minutes=5),
    Timeframe.FIFTEEN_MINUTES: timedelta(minutes=15),
    Timeframe.ONE_HOUR: timedelta(hours=1),
    Timeframe.FOUR_HOURS: timedelta(hours=4),
}


def as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("A timezone offset is required. Use Z for UTC.")
    return value.astimezone(UTC)


def floor_time(value: datetime, timeframe: Timeframe) -> datetime:
    value = as_utc(value)
    return value - (value - EPOCH) % timeframe.duration


def candle_window(
    start: datetime, end: datetime, timeframe: Timeframe
) -> tuple[datetime, datetime]:
    start, end = as_utc(start), as_utc(end)
    if start >= end:
        raise ValueError("start must be before end.")
    if start != floor_time(start, timeframe) or end != floor_time(end, timeframe):
        raise ValueError(f"start and end must align to {timeframe} UTC candle boundaries.")
    return start, end


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: str
    symbol: str
    base_asset: str
    quote_asset: str
    instrument_type: Literal["spot"]


BITCOIN = Market(
    venue="delta_india",
    symbol="BTC_INR",
    base_asset="BTC",
    quote_asset="INR",
    instrument_type="spot",
)


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: str
    symbol: str
    timeframe: Timeframe
    open_time: AwareDatetime
    close_time: AwareDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Volume | None
    is_closed: bool

    @field_validator("open_time", "close_time")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return as_utc(value)

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.open_time != floor_time(self.open_time, self.timeframe):
            raise ValueError("Candle open must align to its UTC timeframe boundary.")
        if self.close_time - self.open_time != self.timeframe.duration:
            raise ValueError("Candle close must be exactly one timeframe after its open.")
        if not (self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError("Candle OHLC values are inconsistent.")
        return self
