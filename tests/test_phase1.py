import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.backfill import main
from app.main import create_app
from app.market.delta import DELTA_URL, DeltaCandleProvider, DeltaError
from app.market.ingestion import backfill
from app.market.models import BITCOIN, Candle, Timeframe
from app.market.storage import CandleStore, StorageError

START = datetime(2026, 11, 20, tzinfo=UTC)
FIVE = Timeframe.FIVE_MINUTES
STEP = FIVE.duration


@pytest.fixture
def migrated_dir(tmp_path: Path) -> Path:
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.attributes["data_dir"] = tmp_path
    command.upgrade(config, "head")
    return tmp_path


@pytest.fixture
def store(migrated_dir: Path):
    storage = CandleStore(migrated_dir)
    try:
        yield storage
    finally:
        storage.close()


def bar(index: int, *, close: str = "101", volume: str | None = "2") -> Candle:
    open_time = START + index * STEP
    return Candle(
        venue=BITCOIN.venue,
        symbol=BITCOIN.symbol,
        timeframe=FIVE,
        open_time=open_time,
        close_time=open_time + STEP,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("98"),
        close=Decimal(close),
        volume=Decimal(volume) if volume is not None else None,
        is_closed=True,
    )


def test_delta_backfill_to_api_is_idempotent(store: CandleStore, migrated_dir: Path):
    seen = []

    def delta_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "api-key" not in request.headers
        if request.url.path == "/v2/products/BTC_INR":
            return httpx.Response(
                200,
                json={"success": True, "result": {"symbol": "BTC_INR", "contract_type": "spot"}},
            )
        assert request.url.path == "/v2/history/candles"
        params = request.url.params
        assert params["symbol"] == "BTC_INR"
        assert params["resolution"] == "5m"
        page_start, api_end = int(params["start"]), int(params["end"])
        seen.append((page_start, api_end))
        row = {
            "time": int(START.timestamp()),
            "open": "precise price",
            "high": 103,
            "low": 98,
            "close": 101,
            "volume": "0.00004599999999999999",
        }
        if page_start == int(START.timestamp()):
            rows = [row, row]
        else:
            row = row | {"time": int((START + 3 * STEP).timestamp()), "volume": None}
            rows = [row | {"time": api_end + 1}, row]
        content = json.dumps({"success": True, "result": rows}).replace(
            '"precise price"', "100.123456789012345678"
        )
        return httpx.Response(200, content=content)

    with httpx.Client(base_url=DELTA_URL, transport=httpx.MockTransport(delta_response)) as client:
        provider = DeltaCandleProvider(client)
        provider.verify_market(BITCOIN)
        reports = [
            backfill(
                store,
                BITCOIN,
                FIVE,
                START,
                START + 5 * STEP,
                provider.fetch_candles,
                page_size=2,
                now=START + 4 * STEP + timedelta(seconds=30),
            )
            for _ in range(2)
        ]

    start_epoch = int(START.timestamp())
    assert seen == [(start_epoch, start_epoch + 599), (start_epoch + 600, start_epoch + 1199)] * 2
    assert [(report.received, report.inserted, report.duplicates) for report in reports] == [
        (3, 2, 1),
        (3, 0, 3),
    ]
    assert reports[0].end == START + 4 * STEP
    assert reports[0].missing == 2
    assert len(reports[0].gaps) == 1
    assert reports[0].gaps[0].start == START + STEP
    assert reports[0].gaps[0].end == START + 3 * STEP
    assert len(list(migrated_dir.glob("candles/5m/*.parquet"))) == 2

    local_offset = "+05:30"
    with TestClient(create_app(migrated_dir)) as api:
        assert api.get("/health").json() == {"status": "ok"}
        assert api.get("/markets").json() == [BITCOIN.model_dump(mode="json")]
        response = api.get(
            "/candles",
            params={"start": f"2026-11-20T05:30:00{local_offset}", "end": "2026-11-20T00:20:00Z"},
        )
        assert response.status_code == 200
        candles = response.json()
        assert [candle["open_time"] for candle in candles] == [
            "2026-11-20T00:00:00Z",
            "2026-11-20T00:15:00Z",
        ]
        assert Decimal(candles[0]["open"]) == Decimal("100.123456789012345678")
        assert Decimal(candles[0]["volume"]) == Decimal("0.00004599999999999999")
        assert candles[1]["volume"] is None
        assert all(candle["is_closed"] for candle in candles)


def test_new_data_can_fill_a_previous_gap(store: CandleStore):
    end = START + 3 * STEP
    store.write_page(BITCOIN, FIVE, START, end, [bar(0), bar(2)])
    report = backfill(store, BITCOIN, FIVE, START, end, lambda *_: [bar(1)], page_size=3, now=end)
    assert report.inserted == 1
    assert report.missing == 0
    assert store.read(BITCOIN, FIVE, START, end) == [bar(0), bar(1), bar(2)]


def test_resampling_cli_skips_incomplete_buckets_and_preserves_missing_volume(
    store: CandleStore,
    migrated_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    end = START + 9 * STEP
    source = [bar(index) for index in (0, 1, 2, 3, 5, 6, 7)] + [bar(8, volume=None, close="102")]
    store.write_page(BITCOIN, FIVE, START, end, source)
    monkeypatch.setenv("TRADEOS_DATA_DIR", str(migrated_dir))

    assert (
        main(
            [
                "--timeframe",
                "15m",
                "--start",
                START.isoformat(),
                "--end",
                end.isoformat(),
                "--resample-from",
                "5m",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["inserted"] == 2
    assert report["missing"] == 1
    many = store.read(BITCOIN, Timeframe.FIFTEEN_MINUTES, START, end)
    assert [candle.open_time for candle in many] == [START, START + 6 * STEP]
    assert (many[0].open, many[0].high, many[0].low, many[0].close, many[0].volume) == (
        Decimal("100"),
        Decimal("103"),
        Decimal("98"),
        Decimal("101"),
        Decimal("6"),
    )
    assert (many[1].close, many[1].volume) == (Decimal("102"), None)


def test_conflict_rejects_the_whole_page_and_retry_succeeds(store: CandleStore):
    end = START + 2 * STEP
    store.write_page(BITCOIN, FIVE, START, end, [bar(1)])

    with pytest.raises(StorageError, match="conflicts"):
        store.write_page(BITCOIN, FIVE, START, end, [bar(0), bar(1, close="102")])
    assert store.read(BITCOIN, FIVE, START, end) == [bar(1)]
    result = store.write_page(BITCOIN, FIVE, START, end, [bar(0), bar(1)])
    assert (result.inserted, result.duplicates) == (1, 1)


def test_concurrent_writers_publish_one_copy(store: CandleStore):
    def write():
        return store.write_page(BITCOIN, FIVE, START, START + STEP, [bar(0)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write)
        second = pool.submit(write)
        assert sorted([first.result().inserted, second.result().inserted]) == [0, 1]
    assert store.read(BITCOIN, FIVE, START, START + STEP) == [bar(0)]


@pytest.mark.parametrize(
    "changes",
    [
        {"high": "99"},
        {"open_time": START.replace(tzinfo=None)},
        {"open_time": START + timedelta(seconds=1)},
        {"close_time": START},
        {"volume": "-1"},
    ],
)
def test_candle_validation(changes):
    with pytest.raises(ValidationError):
        Candle.model_validate(bar(0).model_dump() | changes)


def test_delta_retries_public_reads_and_rejects_futures(monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr("app.market.delta.sleep", lambda _: None)

    def fake_delta(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"symbol": "BTC_INR", "contract_type": "perpetual_futures"},
            },
        )

    with httpx.Client(base_url=DELTA_URL, transport=httpx.MockTransport(fake_delta)) as client:
        with pytest.raises(DeltaError, match="spot product"):
            DeltaCandleProvider(client).verify_market(BITCOIN)
    assert calls == ["/v2/products/BTC_INR"] * 2


def test_candles_api_validates_window(migrated_dir: Path):
    with TestClient(create_app(migrated_dir)) as api:
        assert (
            api.get(
                "/candles",
                params={
                    "start": "2026-11-20T00:00:00",
                    "end": "2026-11-20T00:15:00Z",
                },
            ).status_code
            == 422
        )
        assert (
            api.get(
                "/candles",
                params={
                    "start": "2026-11-20T00:01:00Z",
                    "end": "2026-11-20T00:15:00Z",
                },
            ).status_code
            == 422
        )
        assert (
            api.get(
                "/candles",
                params={
                    "start": "2026-11-01T00:00:00Z",
                    "end": "2026-11-20T00:00:00Z",
                },
            ).status_code
            == 422
        )


def test_health_reports_uninitialized_storage(tmp_path: Path):
    with TestClient(create_app(tmp_path / "missing")) as api:
        assert api.get("/health").status_code == 503
