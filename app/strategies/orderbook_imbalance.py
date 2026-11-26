from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.backtest.models import BASIS_POINTS, SECOND_US, ReplaySettings
from app.market.book_tape import Frame

VERSION = "btcusd_book_imbalance@1.1.0"
STRATEGIES = ("book_only", "trade_confirmed", "flow_filtered")


@dataclass(frozen=True, slots=True)
class Signal:
    index: int
    imbalance: Decimal
    signed_contracts: int
    volume_contracts: int
    spread_bps: Decimal
    episode_start_us: int = 0
    recent_mid_range_bps: Decimal | None = None
    estimated_round_trip_bps: Decimal | None = None


def find_signals(
    frames: list[Frame], settings: ReplaySettings
) -> tuple[list[Signal], list[Signal]]:
    variants = find_signal_variants(frames, settings)
    return variants["book_only"], variants["trade_confirmed"]


def find_signal_variants(frames: list[Frame], settings: ReplaySettings) -> dict[str, list[Signal]]:
    variants: dict[str, list[Signal]] = {name: [] for name in STRATEGIES}
    recent: deque[tuple[int, int]] = deque()
    # Each queue retains the quotes that can still become its rolling extreme.
    low_mids: deque[tuple[int, Decimal]] = deque()
    high_mids: deque[tuple[int, Decimal]] = deque()
    activity_start = None
    activity_us = settings.flow_activity_seconds * SECOND_US
    # Net volume divided by total volume equals twice the buy share minus one.
    buy_share_net_fraction = 2 * settings.flow_min_buy_share - 1
    fee_multiplier = (1 + settings.fee_rate) / (1 - settings.fee_rate)
    total = net = 0
    above_since = None
    emitted_book = emitted_confirmed = emitted_filtered = False
    segment = 0
    last_quote = None
    imbalance = spread = None

    for index, frame in enumerate(frames):
        window_start = frame.time_us - settings.persistence_us
        cutoff = frame.time_us - activity_us
        segment_changed = frame.segment != segment
        if segment_changed:
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
        new_quote = quote is not last_quote
        if new_quote:
            last_quote = quote
            imbalance = quote.imbalance(settings.levels) if quote is not None else None
            spread = quote.spread_bps if quote is not None else None
        stale_between = index > 0 and frame.quote_us - frames[index - 1].quote_us > (
            settings.max_quote_age_us
        )
        fresh_quote = frame.has_quote(settings.max_quote_age_us)
        if segment_changed or stale_between or not fresh_quote:
            activity_start = None
            low_mids.clear()
            high_mids.clear()
        if fresh_quote:
            assert quote is not None
            while low_mids and low_mids[0][0] < cutoff:
                low_mids.popleft()
            while high_mids and high_mids[0][0] < cutoff:
                high_mids.popleft()
            if new_quote or activity_start is None:
                mid = quote.mid
                if activity_start is None:
                    activity_start = frame.quote_us
                while low_mids and low_mids[-1][1] >= mid:
                    low_mids.pop()
                while high_mids and high_mids[-1][1] <= mid:
                    high_mids.pop()
                low_mids.append((frame.quote_us, mid))
                high_mids.append((frame.quote_us, mid))
        if stale_between:
            above_since = None
        if (
            not fresh_quote
            or imbalance is None
            or imbalance <= settings.threshold
            or spread is None
            or spread > settings.max_spread_bps
        ):
            above_since = None
            continue
        if above_since is None:
            above_since = frame.time_us
            emitted_book = emitted_confirmed = emitted_filtered = False
        if frame.time_us - above_since < settings.persistence_us:
            continue
        signal = Signal(index, imbalance, net, total, spread, above_since)
        if not emitted_book:
            variants["book_only"].append(signal)
            emitted_book = True
        if not emitted_confirmed and net > 0:
            variants["trade_confirmed"].append(signal)
            emitted_confirmed = True
        if (
            not emitted_filtered
            and net >= settings.flow_min_net_contracts
            and net >= total * buy_share_net_fraction
            and activity_start is not None
            and activity_start <= cutoff
            and low_mids
            and high_mids
        ):
            assert quote is not None
            mid_range = (high_mids[0][1] - low_mids[0][1]) / quote.mid * BASIS_POINTS
            cost = (quote.ask * fee_multiplier - quote.bid) / quote.mid * BASIS_POINTS
            if mid_range >= cost:
                variants["flow_filtered"].append(
                    Signal(index, imbalance, net, total, spread, above_since, mid_range, cost)
                )
                emitted_filtered = True

    return variants
