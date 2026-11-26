from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.market.orderbook import QUOTE_DEPTH

USD_INR = Decimal(85)
LOT_BTC = Decimal("0.001")
INR_PER_POINT_PER_LOT = USD_INR * LOT_BTC
BASIS_POINTS = Decimal(10_000)
MAINTENANCE_RATE = Decimal("0.0025")
SECOND_US = 1_000_000
FUNDING_INTERVAL_US = 8 * 3_600 * SECOND_US
MAX_MARK_AGE_US = 6 * SECOND_US


class ReplaySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    levels: Annotated[int, Field(ge=1, le=QUOTE_DEPTH)] = 5
    threshold: Annotated[Decimal, Field(ge=0, lt=1)] = Decimal("0.30")
    persistence_ms: Annotated[int, Field(gt=0)] = 2_000
    latency_ms: Annotated[int, Field(gt=0)] = 250
    max_quote_age_ms: Annotated[int, Field(gt=0)] = 2_000
    max_spread_bps: Annotated[Decimal, Field(gt=0)] = Decimal(2)
    flow_min_net_contracts: Annotated[int, Field(gt=0)] = 50
    flow_min_buy_share: Annotated[Decimal, Field(gt=Decimal("0.5"), le=1)] = Decimal("0.65")
    flow_activity_seconds: Annotated[int, Field(gt=0)] = 60
    initial_cash_inr: Annotated[Decimal, Field(gt=0)] = Decimal(100_000)
    margin_inr: Annotated[Decimal, Field(gt=0)] = Decimal(50_000)
    leverage: Annotated[int, Field(ge=1, le=200)] = 25
    stop_points: Annotated[Decimal, Field(gt=0)] = Decimal(200)
    reward_to_risk: Annotated[Decimal, Field(gt=0)] = Decimal(2)
    fee_bps: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("5.9")
    max_hold_seconds: Annotated[int, Field(gt=0, lt=28_800)] = 300

    @model_validator(mode="after")
    def validate_margin(self) -> Self:
        if self.margin_inr > self.initial_cash_inr:
            raise ValueError("Trade margin exceeds initial account equity.")
        if self.margin_inr * self.leverage > 100_000 * USD_INR:
            raise ValueError("Position size exceeds the modeled base maintenance-margin tier.")
        return self

    @property
    def persistence_us(self) -> int:
        return self.persistence_ms * 1_000

    @property
    def latency_us(self) -> int:
        return self.latency_ms * 1_000

    @property
    def max_quote_age_us(self) -> int:
        return self.max_quote_age_ms * 1_000

    @property
    def fee_rate(self) -> Decimal:
        return self.fee_bps / BASIS_POINTS


@dataclass(frozen=True, slots=True)
class Fill:
    contracts: int
    average_usd: Decimal
    notional_inr: Decimal


@dataclass(frozen=True, slots=True)
class Bracket:
    stop_usd: Decimal
    target_usd: Decimal
    liquidation_usd: Decimal
    planned_loss_inr: Decimal
