from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.backtest.models import ReplaySettings
from app.market.book_tape import Frame

VERSION = "btcusd_book_imbalance@1.0.0"


@dataclass(frozen=True, slots=True)
class Signal:
    index: int
    imbalance: Decimal
    signed_contracts: int
    volume_contracts: int
    spread_bps: Decimal
    episode_start_us: int = 0


def find_signals(
    frames: list[Frame], settings: ReplaySettings
) -> tuple[list[Signal], list[Signal]]:
    book_only: list[Signal] = []
    confirmed: list[Signal] = []
    recent: deque[tuple[int, int]] = deque()
    total = net = 0
    above_since = None
    emitted_book = emitted_confirmed = False
    segment = 0
    last_quote = None
    imbalance = spread = None

    for index, frame in enumerate(frames):
        window_start = frame.time_us - settings.persistence_us
        if frame.segment != segment:
            segment = frame.segment
            recent.clear()
            total = net = 0
            above_since = None
            last_quote = None
        while recent and recent[0][0] <= window_start:
            _, signed = recent.popleft()
            net -= signed
            total -= abs(signed)
        if frame.signed_contracts:
            recent.append((frame.time_us, frame.signed_contracts))
            net += frame.signed_contracts
            total += abs(frame.signed_contracts)

        quote = frame.quote
        if quote is not last_quote:
            last_quote = quote
            imbalance = quote.imbalance(settings.levels) if quote is not None else None
            spread = quote.spread_bps if quote is not None else None
        stale_between = index > 0 and frame.quote_us - frames[index - 1].quote_us > (
            settings.max_quote_age_us
        )
        if stale_between:
            above_since = None
        if (
            not frame.has_quote(settings.max_quote_age_us)
            or imbalance is None
            or imbalance <= settings.threshold
            or spread is None
            or spread > settings.max_spread_bps
        ):
            above_since = None
            continue
        if above_since is None:
            above_since = frame.time_us
            emitted_book = emitted_confirmed = False
        if frame.time_us - above_since < settings.persistence_us:
            continue
        signal = Signal(index, imbalance, net, total, spread, above_since)
        if not emitted_book:
            book_only.append(signal)
            emitted_book = True
        if not emitted_confirmed and net > 0:
            confirmed.append(signal)
            emitted_confirmed = True

    return book_only, confirmed
