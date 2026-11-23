import hashlib
import json
from datetime import datetime
from functools import cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.backtest.engine import simulate
from app.backtest.models import BacktestReport, BacktestRequest
from app.backtest.research import research_report, validate_research
from app.database import backtest_runs
from app.market.ingestion import find_gaps
from app.market.models import as_utc
from app.market.storage import CandleStore
from app.strategies.engine import Replay, prepare_replay

MAX_BACKTEST_SLOTS = 200_000


def get_backtest(store: CandleStore, run_id: str) -> BacktestReport | None:
    with store.engine.connect() as connection:
        report = connection.scalar(
            select(backtest_runs.c.report_json).where(backtest_runs.c.id == run_id)
        )
    return BacktestReport.model_validate_json(report) if report is not None else None


def dataset_hash(replay: Replay) -> str:
    digest = hashlib.sha256()
    for source in (replay.signal_bars, replay.context_bars):
        for candle in source.values():
            digest.update(candle.model_dump_json().encode())
            digest.update(b"\n")
    return digest.hexdigest()


def code_hash() -> str:
    app_root = Path(__file__).resolve().parents[1]
    files = sorted(
        [
            *(app_root / "backtest").glob("*.py"),
            *(app_root / "strategies").glob("*.py"),
            *(app_root / "market").glob("*.py"),
        ]
    )
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(app_root).as_posix().encode())
        digest.update(b"\n")
        digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\n")
    return digest.hexdigest()


CODE_SHA256 = code_hash()


def run_backtest(
    store: CandleStore, request: BacktestRequest, *, now: datetime | None = None
) -> BacktestReport:
    replay = prepare_replay(
        store,
        request.start,
        request.end,
        settings=request.pullback_settings,
        strategy_version=request.strategy_version,
        max_slots=MAX_BACKTEST_SLOTS,
        now=now,
    )
    if replay.end != as_utc(request.end):
        raise ValueError("The end must be at or before the latest closed candle boundary.")
    if not replay.candles:
        raise ValueError(f"No stored {replay.timeframe} Bitcoin candles in the requested window.")
    if request.research is not None:
        validate_research(replay, request.research)

    request = request.model_copy(update={"start": replay.start, "end": replay.end})
    dataset_sha = dataset_hash(replay)
    identity = json.dumps(
        [dataset_sha, CODE_SHA256, request.model_dump(mode="json")], sort_keys=True
    )
    run_id = hashlib.sha256(identity.encode()).hexdigest()
    saved = get_backtest(store, run_id)
    if saved is not None:
        return saved

    signal_at = cache(replay.signal_at) if request.research is not None else replay.signal_at
    result = simulate(replay, request.simulation, signal_at=signal_at)
    baseline = simulate(replay, request.simulation, signal_at=None).metrics
    step = replay.timeframe.duration
    gaps = find_gaps(replay.start, replay.end, step, (bar.open_time for bar in replay.candles))
    report = BacktestReport(
        run_id=run_id,
        dataset_sha256=dataset_sha,
        code_sha256=CODE_SHA256,
        strategy_settings_fingerprint=replay.settings_fingerprint,
        timeframe=replay.timeframe,
        request=request,
        expected_candles=(replay.end - replay.start) // step,
        missing_candles=sum(gap.missing for gap in gaps),
        gaps=gaps,
        result=result,
        buy_and_hold=baseline,
        research=research_report(replay, request, request.research, result, signal_at)
        if request.research is not None
        else None,
    )
    with store.engine.begin() as connection:
        connection.execute(
            insert(backtest_runs)
            .values(id=run_id, report_json=report.model_dump_json())
            .on_conflict_do_nothing(index_elements=[backtest_runs.c.id])
        )
    return report
