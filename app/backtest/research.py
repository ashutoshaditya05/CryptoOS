from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean, median

from app.backtest.engine import entry_at, frame_at, simulate
from app.backtest.fills import net_pnl, sweep
from app.backtest.models import BASIS_POINTS, SECOND_US, ReplaySettings
from app.market.book_tape import Frame, Tape
from app.strategies.orderbook_imbalance import VERSION, Signal, find_signals

HORIZONS_SECONDS = (10, 60, 300)


def utc_string(time_us: int) -> str:
    return datetime.fromtimestamp(time_us / SECOND_US, UTC).isoformat()


def markouts(
    frames: list[Frame], times: list[int], signals: list[Signal], settings: ReplaySettings
) -> tuple[dict, list[dict]]:
    stats: dict[int, list[tuple[Decimal, Decimal, Decimal]]] = defaultdict(list)
    missing: dict[int, Counter] = defaultdict(Counter)
    skipped: Counter[str] = Counter()
    rows = []
    for signal in signals:
        row = {
            "signal_us": times[signal.index],
            "episode_start_us": signal.episode_start_us,
            "imbalance": signal.imbalance,
            "taker_net_contracts": signal.signed_contracts,
            "volume_contracts": signal.volume_contracts,
            "signal_spread_bps": signal.spread_bps,
            "outcomes": {},
        }
        rows.append(row)
        entry = entry_at(frames, times, signal, settings)
        if isinstance(entry, str):
            skipped[entry] += 1
            row["entry_status"] = entry
            continue
        index, fill = entry
        opened = frames[index]
        assert opened.quote is not None
        row["entry_us"] = opened.time_us
        row["contracts"] = fill.contracts
        row["entry_average_usd"] = fill.average_usd
        for horizon in HORIZONS_SECONDS:
            bucket = missing[horizon]
            when = opened.time_us + horizon * SECOND_US
            future_index = frame_at(times, index + 1, when, settings)
            if future_index is None:
                reason = "capture_end" if when > times[-1] else "missing_quote_at_horizon"
                bucket[reason] += 1
                row["outcomes"][horizon] = {"status": reason}
                continue
            future = frames[future_index]
            quote = future.quote
            if (
                quote is None
                or future.segment != opened.segment
                or not future.has_quote(settings.max_quote_age_us)
            ):
                bucket["book_gap_or_stale"] += 1
                row["outcomes"][horizon] = {"status": "book_gap_or_stale"}
                continue
            exit_ = sweep(quote.bids, fill.contracts)
            if exit_ is None:
                bucket["insufficient_exit_depth"] += 1
                row["outcomes"][horizon] = {"status": "insufficient_exit_depth"}
                continue
            mid_move = quote.mid - opened.quote.mid
            net = net_pnl(fill, exit_, settings.fee_rate)
            net_bps = net / fill.notional_inr * BASIS_POINTS
            stats[horizon].append((mid_move, net, net_bps))
            row["outcomes"][horizon] = {
                "exit_us": future.time_us,
                "mid_move_usd": mid_move,
                "net_pnl_inr": net,
                "net_bps": net_bps,
            }

    horizons = {}
    for horizon in HORIZONS_SECONDS:
        samples = stats[horizon]
        result = {"observed": len(samples), "missing": dict(missing[horizon])}
        if samples:
            moves, profits, bps = zip(*samples, strict=True)
            result |= {
                "mean_mid_move_usd": mean(moves),
                "positive_mid_moves": sum(x > 0 for x in moves),
                "mean_net_pnl_inr": mean(profits),
                "median_net_pnl_inr": median(profits),
                "best_net_pnl_inr": max(profits),
                "worst_net_pnl_inr": min(profits),
                "mean_net_bps": mean(bps),
                "positive_net": sum(x > 0 for x in profits),
            }
        horizons[horizon] = result
    return {"entry_skips": dict(skipped), "horizons_seconds": horizons}, rows


def compare_episodes(book: list[dict], confirmed: list[dict]) -> dict:
    by_episode = {row["episode_start_us"]: row for row in book}
    pairs = [(by_episode[row["episode_start_us"]], row) for row in confirmed]
    horizons = {}
    for horizon in HORIZONS_SECONDS:
        differences = []
        for baseline, filtered in pairs:
            base = baseline["outcomes"].get(horizon, {}).get("net_bps")
            net = filtered["outcomes"].get(horizon, {}).get("net_bps")
            if base is not None and net is not None:
                differences.append(net - base)
        horizons[horizon] = {
            "paired_observations": len(differences),
            "mean_confirmed_minus_book_net_bps": mean(differences) if differences else None,
        }
    return {
        "unconfirmed_episodes": len(book) - len(confirmed),
        "confirmed_episodes": len(confirmed),
        "identical_signal_times": sum(a["signal_us"] == b["signal_us"] for a, b in pairs),
        "horizons_seconds": horizons,
    }


def evaluate(frames: list[Frame], settings: ReplaySettings, *, ledger: bool = False) -> dict:
    times = [frame.time_us for frame in frames]
    book_only, confirmed = find_signals(frames, settings)
    result = {}
    ledgers = []
    for name, signals in (("book_only", book_only), ("trade_confirmed", confirmed)):
        summary, trades = simulate(frames, times, signals, settings)
        forecasts, rows = markouts(frames, times, signals, settings)
        ledgers.append(rows)
        result[name] = {"portfolio": summary, "markouts": forecasts}
        if ledger:
            result[name] |= {"trades": trades, "signal_ledger": rows}
    result["paired_episode_comparison"] = compare_episodes(*ledgers)
    return result


def research(tape: Tape, settings: ReplaySettings, *, ledger: bool = False) -> dict:
    frames = tape.frames
    start, end = frames[0].time_us, frames[-1].time_us
    report = {
        "strategy_version": VERSION,
        "venue": "delta_india",
        "symbol": "BTCUSD",
        "capture_sha256": tape.sha256,
        "capture_start_utc": utc_string(start),
        "capture_end_utc": utc_string(end),
        "capture_seconds": Decimal(end - start) / SECOND_US,
        "data_quality": tape.quality,
        "settings": settings.model_dump(mode="json"),
        "full_window": evaluate(frames, settings, ledger=ledger),
    }
    split = start + (end - start) * 4 // 5
    index = bisect_left([frame.time_us for frame in frames], split)
    if 0 < index < len(frames):
        report["chronological_windows"] = {
            "split_utc": utc_string(split),
            "earlier": evaluate(frames[:index], settings),
            "later": evaluate(frames[index:], settings),
        }
    report["latency_sensitivity_ms"] = {
        delay: evaluate(frames, settings.model_copy(update={"latency_ms": delay}))
        for delay in (100, 500)
        if delay != settings.latency_ms
    }
    return report
