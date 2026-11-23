import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.market.models import BITCOIN, Candle, Timeframe
from app.market.storage import CandleStore
from app.signals import main
from app.strategies.engine import generate_signals
from app.strategies.models import Position, Regime
from app.strategies.vwap_ema_cross import VwapEmaCross, session_vwap

DAY = datetime(2026, 11, 22, tzinfo=UTC)
FRAME = Timeframe.THREE_MINUTES
STEP = FRAME.duration
VERSION = VwapEmaCross.version


def bar(
    index: int,
    *,
    open_price: int = 100,
    high: int = 101,
    low: int = 97,
    close: int = 100,
    volume: Decimal | None = Decimal(1),
) -> Candle:
    open_time = DAY + index * STEP
    return Candle(
        venue=BITCOIN.venue,
        symbol=BITCOIN.symbol,
        timeframe=FRAME,
        open_time=open_time,
        close_time=open_time + STEP,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        is_closed=True,
    )


def seed(store: CandleStore, *, missing_volume: bool = False, missing_midnight: bool = False):
    previous_day = bar(-1, high=1000, volume=Decimal(1000))
    first = int(missing_midnight)
    bars = [
        bar(index, volume=None if missing_volume and index == 4 else Decimal(1))
        for index in range(first, 8)
    ]
    entry = bar(8, high=106, low=98, close=105)
    exit_bar = bar(9, open_price=103, high=104, low=98, close=99)
    store.write_page(
        BITCOIN, FRAME, DAY - STEP, DAY + 10 * STEP, [previous_day, *bars, entry, exit_bar]
    )


def test_typical_price_vwap_uses_trade_volume():
    low_price = bar(0, open_price=100, high=100, low=100, close=100, volume=Decimal(1))
    high_price = bar(1, open_price=110, high=130, low=100, close=130, volume=Decimal(3))
    assert session_vwap([low_price, high_price]) == Decimal(115)
    assert session_vwap([bar(0, volume=None)]) is None
    assert session_vwap([bar(0, volume=Decimal(0))]) is None


def test_daily_reset_wick_entry_and_long_exit_use_closed_three_minute_bars(store: CandleStore):
    seed(store)
    start, end = DAY + 8 * STEP, DAY + 10 * STEP
    flat = generate_signals(store, start, end, strategy_version=VERSION, now=end)
    assert [signal.action for signal in flat] == ["BUY", "HOLD"]
    assert flat[0].entry_reference == Decimal(105)
    assert flat[0].reason_codes == ("VWAP_LOWER_WICK", "BULLISH_EMA9_CROSS")
    assert flat[0].timestamp == DAY + 9 * STEP
    assert flat[0].valid_until == end
    assert flat[0].timeframe == FRAME
    assert flat[0].stop_loss is None and flat[0].take_profit is None

    early = generate_signals(
        store, start, end, strategy_version=VERSION, now=end - timedelta(seconds=1)
    )
    assert early == flat[:1]
    overlap = generate_signals(store, DAY, end, strategy_version=VERSION, now=end)
    assert overlap[-2:] == flat
    long = generate_signals(
        store, start, end, strategy_version=VERSION, position=Position.LONG, now=end
    )
    assert [signal.action for signal in long] == ["HOLD", "SELL"]
    assert long[-1].reason_codes == ("CLOSE_BELOW_EMA9",)


@pytest.mark.parametrize(
    ("missing", "reason"),
    [("volume", "VWAP_VOLUME_UNAVAILABLE"), ("midnight", "INCOMPLETE_UTC_VWAP_SESSION")],
)
def test_flat_entry_requires_a_complete_session(store: CandleStore, missing: str, reason: str):
    seed(store, missing_volume=missing == "volume", missing_midnight=missing == "midnight")
    start, end = DAY + 9 * STEP, DAY + 10 * STEP
    [flat] = generate_signals(store, start, end, strategy_version=VERSION, now=end)
    assert (flat.action, flat.regime, flat.reason_codes) == (
        "HOLD",
        Regime.DATA_UNAVAILABLE,
        (reason,),
    )
    [long] = generate_signals(
        store, start, end, strategy_version=VERSION, position=Position.LONG, now=end
    )
    assert long.action == "SELL"


def test_clip_strategy_is_selectable_in_cli_and_api(
    store: CandleStore,
    migrated_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    seed(store)
    monkeypatch.setenv("TRADEOS_DATA_DIR", str(migrated_dir))
    start, end = (DAY + 8 * STEP).isoformat(), (DAY + 10 * STEP).isoformat()
    assert main(["--strategy-version", VERSION, "--start", start, "--end", end]) == 0
    output = json.loads(capsys.readouterr().out)

    async def check_api() -> None:
        app = create_app(migrated_dir)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api,
        ):
            response = await api.get(
                "/signals", params={"strategy_version": VERSION, "start": start, "end": end}
            )
            assert response.status_code == 200
            assert response.json() == output

    asyncio.run(check_api())
