import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.backtest.__main__ import main
from app.backtest.engine import simulate
from app.backtest.models import BacktestRequest, ResearchSettings, SimulationSettings
from app.backtest.research import block_bootstrap
from app.backtest.service import get_backtest, run_backtest
from app.main import create_app
from app.market.models import BITCOIN, Candle, Timeframe
from app.market.storage import CandleStore
from app.strategies.engine import prepare_replay
from app.strategies.vwap_ema_cross import VwapEmaCross

DAY = datetime(2026, 11, 22, tzinfo=UTC)
FRAME = Timeframe.THREE_MINUTES
STEP = FRAME.duration
START, END = DAY + 8 * STEP, DAY + 11 * STEP


def bar(index: int, values: tuple[int, int, int, int] = (100, 101, 97, 100), volume="1") -> Candle:
    start = DAY + index * STEP
    open_, high, low, close = map(Decimal, values)
    return Candle(
        venue=BITCOIN.venue,
        symbol=BITCOIN.symbol,
        timeframe=FRAME,
        open_time=start,
        close_time=start + STEP,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=Decimal(volume),
        is_closed=True,
    )


def seed(
    store: CandleStore,
    *,
    gap: bool = False,
    last: int = 11,
    entry_volume: str = "1",
    prior_volume: str = "1",
):
    candles = [bar(index) for index in range(last)]
    candles[7] = bar(7, volume=prior_volume)
    candles[8] = bar(8, (100, 106, 98, 105), volume=entry_volume)
    candles[9] = bar(9, (104, 115, 90, 99))
    candles[10] = bar(10, (96, 102, 95, 100))
    if gap:
        del candles[9]
    store.write_page(BITCOIN, FRAME, DAY, DAY + last * STEP, candles)


def execution(**updates) -> SimulationSettings:
    return SimulationSettings.model_validate(
        {
            "initial_cash_inr": "100",
            "spread_bps": 0,
            "slippage_bps": 0,
            "buy_fee_bps": 100,
            "sell_fee_bps": 100,
            "quantity_step_btc": ".01",
            "min_quantity_btc": ".01",
            "max_order_btc": "1",
            **updates,
        }
    )


def test_next_open_partial_fill_and_round_trip_accounting(store: CandleStore):
    seed(store)
    request = BacktestRequest(start=START, end=END, simulation=execution())
    report = run_backtest(store, request, now=END)
    result = report.result
    buy, sell = result.orders
    assert (buy.side, buy.status, buy.requested_btc) == ("BUY", "PARTIALLY_FILLED", Decimal(".23"))
    assert buy.fill is not None and sell.fill is not None
    assert buy.decision_at == buy.fill.timestamp == DAY + 9 * STEP
    assert buy.fill.reference_price_inr == buy.fill.price_inr == 104
    assert buy.fill.quantity_btc == Decimal(".10")
    assert result.equity_curve[1].position_btc == 0
    assert sell.side == "SELL" and sell.fill.timestamp == DAY + 10 * STEP
    assert sell.fill.price_inr == 96
    assert sell.fill.position_after_btc == 0
    assert result.metrics.fees_inr == Decimal(".20")
    assert result.metrics.final_equity_inr == Decimal(99)
    assert result.metrics.net_pnl_inr == result.metrics.realized_pnl_inr == Decimal(-1)
    assert result.metrics.unrealized_pnl_inr == 0
    assert result.metrics.profit_factor == 0
    assert result.trades[0].net_pnl_inr == Decimal(-1)
    assert result.trades[0].return_on_equity == Decimal("-.01")
    assert report.buy_and_hold.fills == 1
    assert get_backtest(store, report.run_id) == report == run_backtest(store, request, now=END)


def test_missing_next_candle_cancels_instead_of_replaying_at_a_later_open(store: CandleStore):
    seed(store, gap=True)
    report = run_backtest(store, BacktestRequest(start=START, end=END), now=END)
    assert report.missing_candles == 1 and report.gaps[0].start == DAY + 9 * STEP
    [order] = report.result.orders
    assert (order.status, order.reason, order.fill) == ("CANCELLED", "MISSING_NEXT_CANDLE", None)
    assert report.result.metrics.fills == 0


def test_benchmark_waits_for_a_fillable_open(store: CandleStore):
    seed(store, prior_volume="0")
    report = run_backtest(store, BacktestRequest(start=START, end=END), now=END)
    assert report.buy_and_hold.fills == 1
    assert report.buy_and_hold.orders == 2
    assert report.buy_and_hold.first_fill_at == DAY + 9 * STEP


def test_one_year_window_preserves_sparse_coverage_counts(store: CandleStore):
    seed(store)
    request = BacktestRequest(start=END - timedelta(days=365), end=END)
    report = run_backtest(store, request, now=END)
    assert report.expected_candles == 365 * 480
    assert report.result.candles == 11
    assert report.missing_candles == report.expected_candles - 11


@pytest.mark.parametrize(
    ("minimum", "reason"), [(15, "INSUFFICIENT_PRIOR_VOLUME"), (30, "BELOW_ORDER_MINIMUM")]
)
def test_minimum_notional_applies_to_orders_and_partial_fills(
    store: CandleStore, minimum: int, reason: str
):
    seed(store)
    request = BacktestRequest(start=START, end=END, simulation=execution(min_notional_inr=minimum))
    report = run_backtest(store, request, now=END)
    [order] = report.result.orders
    assert order.fill is None and order.reason == reason
    assert report.result.metrics.final_equity_inr == 100


def test_no_liquidity_and_terminal_order_remain_unfilled(store: CandleStore):
    seed(store, entry_volume="0")
    report = run_backtest(store, BacktestRequest(start=START, end=END), now=END)
    [order] = report.result.orders
    assert order.reason == "INSUFFICIENT_PRIOR_VOLUME" and order.fill is None
    single = run_backtest(store, BacktestRequest(start=START, end=START + STEP), now=END)
    assert single.result.orders[0].reason == "END_OF_WINDOW"
    assert single.result.metrics.net_pnl_inr == 0


def test_same_bar_protective_collision_uses_stop_after_next_open_entry(store: CandleStore):
    seed(store)
    replay = prepare_replay(store, START, END, strategy_version=VwapEmaCross.version, now=END)

    def with_bracket(timestamp, position):
        signal = replay.signal_at(timestamp, position)
        if signal.action == "BUY":
            return signal.model_copy(update={"stop_loss": Decimal(95), "take_profit": Decimal(110)})
        return signal

    simulation = simulate(replay, execution(volume_participation="1"), signal_at=with_bracket)
    buy, stop = simulation.orders
    assert buy.fill is not None and stop.fill is not None
    assert buy.fill.execution == "OPEN" and buy.fill.timestamp == DAY + 9 * STEP
    assert (stop.trigger, stop.fill.execution, stop.fill.reference_price_inr) == (
        "STOP_LOSS",
        "INTRABAR",
        Decimal(95),
    )
    assert stop.fill.timestamp == DAY + 10 * STEP
    assert simulation.metrics.closed_trades == 1


def test_research_windows_and_seeded_bootstrap(store: CandleStore):
    seed(store, last=30)
    settings = ResearchSettings(bootstrap_paths=100)
    report = run_backtest(
        store,
        BacktestRequest(start=DAY, end=DAY + 30 * STEP, research=settings),
        now=DAY + 30 * STEP,
    )
    research = report.research
    assert research is not None
    train, validation, holdout = research.chronological
    assert train.end == validation.start and validation.end == holdout.start
    assert holdout.selection_end == train.end
    assert all(
        fold.selection_end == fold.start and fold.end <= holdout.start
        for fold in research.walk_forward
    )
    assert research.cost_sensitivity["base"] == report.result.metrics
    assert research.block_bootstrap is None
    returns = [Decimal(value) for value in (".1", "-.05", ".2", "-.03", ".02", "-.04")]
    distribution = block_bootstrap(returns, settings)
    assert distribution == block_bootstrap(returns, settings)
    assert distribution is not None
    assert distribution.return_p05 <= distribution.return_p50 <= distribution.return_p95
    assert 0 <= distribution.drawdown_p95 < 1


def test_backtest_cli_and_api_store_the_same_report(
    store: CandleStore,
    migrated_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    seed(store)
    monkeypatch.setenv("TRADEOS_DATA_DIR", str(migrated_dir))
    start, end = START.isoformat(), END.isoformat()
    assert main(["--start", start, "--end", end, "--full"]) == 0
    cli = json.loads(capsys.readouterr().out)

    async def check_api():
        app = create_app(migrated_dir)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api,
        ):
            response = await api.post("/backtests", json={"start": start, "end": end})
            assert response.status_code == 200
            assert response.json() == cli
            saved = await api.get(f"/backtests/{cli['run_id']}")
            assert saved.json() == cli
            assert (await api.get("/backtests/unknown")).status_code == 404
            future = (DAY + timedelta(days=4000)).isoformat()
            invalid = await api.post("/backtests", json={"start": future, "end": future})
            assert invalid.status_code == 422

    asyncio.run(check_api())
