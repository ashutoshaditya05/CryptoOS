from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.market.ingestion import Gap
from app.market.models import Timeframe
from app.strategies.models import PullbackSettings
from app.strategies.vwap_ema_cross import VwapEmaCross

type Bps = Annotated[Decimal, Field(ge=0, le=2_000, max_digits=12, decimal_places=6)]
type Fraction = Annotated[Decimal, Field(gt=0, le=1, max_digits=12, decimal_places=10)]
type Quantity = Annotated[Decimal, Field(gt=0, le=1, max_digits=12, decimal_places=10)]
type Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]
type Side = Literal["BUY", "SELL"]


class SimulationSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_cash_inr: Annotated[Money, Field(gt=0)] = Decimal("100000")
    allocation: Fraction = Decimal("0.25")
    buy_fee_bps: Bps = Decimal(0)
    sell_fee_bps: Bps = Decimal(10)
    spread_bps: Bps = Decimal(10)
    slippage_bps: Bps = Decimal(5)
    volume_participation: Fraction = Decimal("0.10")
    quantity_step_btc: Quantity = Decimal("0.000001")
    min_quantity_btc: Quantity = Decimal("0.000001")
    max_order_btc: Quantity = Decimal("0.25")
    min_notional_inr: Money = Decimal(0)
    price_tick_inr: Annotated[Money, Field(gt=0)] = Decimal("0.01")


class ResearchSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap_paths: Annotated[int, Field(ge=100, le=2_000)] = 500
    block_size: Annotated[int, Field(ge=1, le=20)] = 3
    seed: int = 42
    walk_forward_folds: Annotated[int, Field(ge=2, le=6)] = 3
    pullback_candidates: Annotated[tuple[PullbackSettings, ...], Field(max_length=5)] = ()


class BacktestRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: AwareDatetime
    end: AwareDatetime
    strategy_version: str = VwapEmaCross.version
    pullback_settings: PullbackSettings | None = None
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    research: ResearchSettings | None = None


class Fill(BaseModel):
    timestamp: datetime
    execution: Literal["OPEN", "INTRABAR"]
    quantity_btc: Decimal
    reference_price_inr: Decimal
    price_inr: Decimal
    notional_inr: Decimal
    fee_inr: Decimal
    spread_slippage_inr: Decimal
    cash_after_inr: Decimal
    position_after_btc: Decimal


class Order(BaseModel):
    order_id: int
    side: Side
    decision_at: datetime
    signal_id: str | None
    trigger: str
    status: Literal["FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"]
    reason: str | None = None
    requested_btc: Decimal | None = None
    fill: Fill | None = None


class Trade(BaseModel):
    opened_at: datetime
    closed_at: datetime
    quantity_btc: Decimal
    entry_cost_inr: Decimal
    net_pnl_inr: Decimal
    return_on_entry: Decimal
    return_on_equity: Decimal


class EquityPoint(BaseModel):
    timestamp: datetime
    cash_inr: Decimal
    position_btc: Decimal
    mark_price_inr: Decimal | None
    equity_inr: Decimal
    drawdown: Decimal


class Metrics(BaseModel):
    initial_equity_inr: Decimal
    final_equity_inr: Decimal
    net_pnl_inr: Decimal
    realized_pnl_inr: Decimal
    unrealized_pnl_inr: Decimal
    return_fraction: Decimal
    max_drawdown: Decimal
    first_fill_at: datetime | None = None
    last_mark_at: datetime | None
    fees_inr: Decimal
    spread_slippage_inr: Decimal
    turnover: Decimal
    close_exposure: Decimal
    orders: int
    fills: int
    closed_trades: int
    win_rate: Decimal | None
    net_expectancy_inr: Decimal | None
    profit_factor: Decimal | None
    daily_return_samples: int
    annualized_volatility: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    daily_var_95: Decimal | None


class SimulationResult(BaseModel):
    metrics: Metrics
    orders: list[Order]
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    candles: int
    unavailable_signals: int
    buy_signals: int
    sell_signals: int


class WindowResult(BaseModel):
    name: str
    start: datetime
    end: datetime
    selection_start: datetime | None = None
    selection_end: datetime | None = None
    selected_settings: PullbackSettings | None = None
    metrics: Metrics


class BootstrapResult(BaseModel):
    paths: int
    block_size: int
    seed: int
    trade_samples: int
    return_p05: Decimal
    return_p50: Decimal
    return_p95: Decimal
    drawdown_p95: Decimal


class ResearchResult(BaseModel):
    chronological: list[WindowResult]
    walk_forward: list[WindowResult]
    cost_sensitivity: dict[str, Metrics]
    block_bootstrap: BootstrapResult | None


class BacktestReport(BaseModel):
    run_id: str
    dataset_sha256: str
    code_sha256: str
    strategy_settings_fingerprint: str
    symbol: Literal["BTC_INR"] = "BTC_INR"
    timeframe: Timeframe
    request: BacktestRequest
    expected_candles: int
    missing_candles: int
    gaps: list[Gap]
    result: SimulationResult
    buy_and_hold: Metrics
    research: ResearchResult | None = None
