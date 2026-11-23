import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.market.indicators import atr_adx, ema
from app.market.models import BITCOIN, Candle, Timeframe
from app.market.storage import CandleStore
from app.signals import main
from app.strategies.engine import generate_signals
from app.strategies.models import Position, PullbackSettings, Regime

START = datetime(2026, 11, 20, tzinfo=UTC)
SIGNAL_FRAME = Timeframe.FIFTEEN_MINUTES
HOUR_FRAME = Timeframe.ONE_HOUR
STEP = SIGNAL_FRAME.duration
END = START + HOUR_FRAME.duration


def bar(
    when: datetime, frame: Timeframe, close: Decimal, *, width: int = 1, pullback: int = 1
) -> Candle:
    return Candle(
        venue=BITCOIN.venue,
        symbol=BITCOIN.symbol,
        timeframe=frame,
        open_time=when,
        close_time=when + frame.duration,
        open=close,
        high=close + width,
        low=close - pullback,
        close=close,
        volume=None,
        is_closed=True,
    )


def seed_replay(store: CandleStore, *, hour_gap: bool = False, signal_gap: bool = False) -> None:
    hourly_start = START - 620 * HOUR_FRAME.duration
    hourly = [
        bar(hourly_start + i * HOUR_FRAME.duration, HOUR_FRAME, Decimal(1000) + Decimal(i) / 100)
        for i in range(620 - int(hour_gap))
    ]
    hourly.append(bar(START, HOUR_FRAME, Decimal(120)))
    store.write_page(BITCOIN, HOUR_FRAME, hourly_start, END, hourly)

    first_signal = START - 160 * STEP
    signal_bars = [
        bar(
            first_signal + i * STEP,
            SIGNAL_FRAME,
            Decimal(850 + i),
            pullback=12 if i in (160, 162) else 2,
        )
        for i in range(164)
        if not (signal_gap and i == 161)
    ]
    store.write_page(BITCOIN, SIGNAL_FRAME, first_signal, END, signal_bars)


@pytest.fixture
def frozen_replay(store: CandleStore) -> CandleStore:
    seed_replay(store)
    return store


def test_indicators_seed_and_smooth():
    closes = [Decimal(value) for value in (1, 2, 3, 6, 2)]
    assert ema(closes, 3) == Decimal(3)
    assert ema(closes[:2], 3) is None

    candles = [
        bar(START + index * STEP, SIGNAL_FRAME, Decimal(close))
        for index, close in enumerate((10, 12, 11, 14))
    ]
    assert atr_adx(candles[:2], 2) == (None, None)
    assert atr_adx(candles[:3], 2) == (Decimal("2.5"), None)
    atr, adx = atr_adx(candles, 2)
    assert atr == Decimal("3.25")
    assert adx is not None
    assert adx.quantize(Decimal("0.00000001")) == Decimal("55.55555556")
    flat = [
        bar(START + index * STEP, SIGNAL_FRAME, Decimal(10), width=0, pullback=0)
        for index in range(4)
    ]
    assert atr_adx(flat, 2) == (Decimal(0), Decimal(0))


def test_frozen_replay_matches_golden(frozen_replay: CandleStore):
    settings = PullbackSettings()
    flat = generate_signals(frozen_replay, START, END, settings=settings, now=END)
    long = generate_signals(
        frozen_replay, END - STEP, END, settings=settings, position=Position.LONG, now=END
    )
    actual = {
        "flat": [signal.model_dump(mode="json") for signal in flat],
        "long_at_hour_close": long[0].model_dump(mode="json"),
    }
    fixture = Path(__file__).with_name("fixtures") / "ema_pullback_v1_signals.json"
    assert actual == json.loads(fixture.read_text(encoding="utf-8"))
    assert flat == generate_signals(frozen_replay, START, END, settings=settings, now=END)
    assert all(signal.timestamp + STEP == signal.valid_until for signal in flat)
    assert len({signal.signal_id for signal in [*flat, *long]}) == 5


def test_replay_cannot_see_unclosed_or_later_candles(frozen_replay: CandleStore):
    settings = PullbackSettings()
    full = generate_signals(frozen_replay, START, END, settings=settings, now=END)
    early = generate_signals(
        frozen_replay, START, END, settings=settings, now=END - timedelta(seconds=1)
    )
    overlap = generate_signals(frozen_replay, START + STEP, END, settings=settings, now=END)
    assert early == full[:3]
    assert overlap == full[1:]
    assert [signal.action for signal in full] == ["BUY", "HOLD", "BUY", "HOLD"]
    assert full[-1].regime == Regime.MIXED_TREND
    assert generate_signals(frozen_replay, START, END, settings=settings, now=START) == []


def test_gaps_prevent_entries_and_missing_candles_do_not_emit_signals(store: CandleStore):
    seed_replay(store, hour_gap=True, signal_gap=True)
    result = generate_signals(store, START, END, settings=PullbackSettings(), now=END)
    assert [signal.timestamp for signal in result] == [START + STEP, START + 3 * STEP, END]
    assert all(
        signal.action == "HOLD" and signal.regime == Regime.DATA_UNAVAILABLE for signal in result
    )
    assert result[0].reason_codes == ("INSUFFICIENT_HOURLY_HISTORY",)
    assert result[1].reason_codes == ("INSUFFICIENT_HOURLY_HISTORY", "INSUFFICIENT_SIGNAL_HISTORY")


def test_settings_are_configurable_and_keep_identifiers_deterministic(frozen_replay: CandleStore):
    default = generate_signals(frozen_replay, START, END, settings=PullbackSettings(), now=END)
    strict = generate_signals(
        frozen_replay,
        START,
        END,
        settings=PullbackSettings(max_atr_fraction=Decimal("0.001")),
        now=END,
    )
    assert strict[0].action == "HOLD"
    assert strict[0].regime == Regime.HIGH_VOLATILITY
    assert strict[0].correlation_id == default[0].correlation_id
    assert strict[0].signal_id != default[0].signal_id
    adx_gate = generate_signals(
        frozen_replay, START, START + STEP, settings=PullbackSettings(min_adx=100), now=END
    )
    assert adx_gate[0].action == "HOLD" and adx_gate[0].regime == Regime.SIDEWAYS
    with localcontext(prec=3):
        equivalent = generate_signals(
            frozen_replay,
            START,
            END,
            settings=PullbackSettings(min_adx=Decimal("20.0000")),
            now=END,
        )
    assert equivalent == default


def test_cli_and_api_share_the_same_signals(
    frozen_replay: CandleStore,
    migrated_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setenv("TRADEOS_DATA_DIR", str(migrated_dir))
    assert main(["--start", START.isoformat(), "--end", END.isoformat(), "--min-adx", "21"]) == 0
    cli_output = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit) as error:
        main(["--start", START.isoformat(), "--end", END.isoformat(), "--min-adx", "bad"])
    assert error.value.code == 2
    assert "min_adx" in capsys.readouterr().err

    async def check_api() -> None:
        app = create_app(migrated_dir)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api,
        ):
            params = {"start": START.isoformat(), "end": END.isoformat(), "min_adx": "21"}
            response = await api.get("/signals", params=params)
            assert response.status_code == 200, response.text
            assert response.json() == cli_output
            for invalid in (
                {"min_adx": "101"},
                {"strategy_version": "unknown"},
                {"position": "short"},
            ):
                assert (await api.get("/signals", params=params | invalid)).status_code == 422

    asyncio.run(check_api())
