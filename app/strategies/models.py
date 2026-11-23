import hashlib
from decimal import Context, Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.market.models import Price, Timeframe


class Position(StrEnum):
    FLAT = "flat"
    LONG = "long"


class Regime(StrEnum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    SIDEWAYS = "SIDEWAYS"
    MIXED_TREND = "MIXED_TREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class PullbackSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_adx: Annotated[Decimal, Field(ge=0, le=100, max_digits=10, decimal_places=7)] = Decimal(20)
    max_atr_fraction: Annotated[Decimal, Field(gt=0, le=1, max_digits=10, decimal_places=7)] = (
        Decimal("0.03")
    )
    atr_stop_multiple: Annotated[Decimal, Field(gt=0, le=100, max_digits=10, decimal_places=7)] = (
        Decimal("1.5")
    )
    reward_risk_ratio: Annotated[Decimal, Field(gt=0, le=100, max_digits=10, decimal_places=7)] = (
        Decimal(2)
    )

    def fingerprint(self) -> str:
        context = Context(prec=10)
        fields = "|".join(
            f"{name}={(getattr(self, name) or Decimal(0)).normalize(context):f}"
            for name in PullbackSettings.model_fields
        )
        return hashlib.sha256(fields.encode()).hexdigest()


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["BUY", "SELL", "HOLD"]
    regime: Regime
    reason_codes: tuple[str, ...]
    entry_reference: Price | None = None
    stop_loss: Price | None = None
    take_profit: Price | None = None


class Signal(Decision):
    signal_id: str
    correlation_id: str
    schema_version: Literal[1] = 1
    source: Literal["tradeos.strategy"] = "tradeos.strategy"
    strategy_version: str
    settings_fingerprint: str
    venue: str
    symbol: str
    timeframe: Timeframe
    position: Position
    timestamp: AwareDatetime
    valid_until: AwareDatetime
