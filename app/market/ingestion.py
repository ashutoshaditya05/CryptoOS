from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from app.market.models import Candle, Market, Timeframe, candle_window, floor_time
from app.market.storage import CandleStore

type FetchCandles = Callable[[Market, Timeframe, datetime, datetime], list[Candle]]


class Gap(BaseModel):
    start: datetime
    end: datetime
    missing: int


class BackfillReport(BaseModel):
    market: Market
    timeframe: Timeframe
    start: datetime
    end: datetime
    pages: int = 0
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    missing: int = 0
    gaps: list[Gap] = Field(default_factory=list)


def find_gaps(
    start: datetime, end: datetime, step: timedelta, open_times: Iterable[datetime]
) -> list[Gap]:
    gaps = []
    expected = start
    for open_time in open_times:
        if open_time > expected:
            gaps.append(Gap(start=expected, end=open_time, missing=(open_time - expected) // step))
        expected = open_time + step
    if expected < end:
        gaps.append(Gap(start=expected, end=end, missing=(end - expected) // step))
    return gaps


def backfill(
    store: CandleStore,
    market: Market,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    fetch: FetchCandles,
    *,
    page_size: int,
    now: datetime | None = None,
) -> BackfillReport:
    start, end = candle_window(start, end, timeframe)
    end = min(end, floor_time(now if now is not None else datetime.now(UTC), timeframe))
    if start >= end:
        raise ValueError("The requested range contains no fully closed candles.")
    if page_size < 1:
        raise ValueError("Page size must be positive.")

    report = BackfillReport(market=market, timeframe=timeframe, start=start, end=end)
    page_start = start
    step = timeframe.duration
    while page_start < end:
        page_end = page_start + min(end - page_start, page_size * step)
        candles = fetch(market, timeframe, page_start, page_end)
        written = store.write_page(market, timeframe, page_start, page_end, candles)
        report.pages += 1
        report.received += len(candles)
        report.inserted += written.inserted
        report.duplicates += written.duplicates
        for gap in find_gaps(page_start, page_end, step, written.open_times):
            report.missing += gap.missing
            if report.gaps and report.gaps[-1].end == gap.start:
                previous = report.gaps[-1]
                previous.end = gap.end
                previous.missing += gap.missing
            else:
                report.gaps.append(gap)
        page_start = page_end
    return report
