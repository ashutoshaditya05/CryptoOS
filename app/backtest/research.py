from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

from app.backtest.engine import entry_at, frame_at, simulate
from app.backtest.fills import net_pnl, sweep
from app.backtest.models import BASIS_POINTS, SECOND_US, ReplaySettings
from app.market.book_tape import Frame, Tape, load_tape
from app.strategies.orderbook_imbalance import STRATEGIES, VERSION, Signal, find_signal_variants

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
        if signal.recent_mid_range_bps is not None:
            row["recent_mid_range_bps"] = signal.recent_mid_range_bps
            row["estimated_round_trip_bps"] = signal.estimated_round_trip_bps
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
    result = {}
    ledgers = {}
    for name, signals in find_signal_variants(frames, settings).items():
        summary, trades = simulate(frames, times, signals, settings)
        forecasts, rows = markouts(frames, times, signals, settings)
        ledgers[name] = rows
        result[name] = {"portfolio": summary, "markouts": forecasts}
        if ledger:
            result[name] |= {"trades": trades, "signal_ledger": rows}
    result["paired_episode_comparison"] = compare_episodes(
        ledgers["book_only"], ledgers["trade_confirmed"]
    )
    return result


def _capture_report(tape: Tape, settings: ReplaySettings, *, ledger: bool) -> dict:
    start, end = tape.frames[0].time_us, tape.frames[-1].time_us
    return {
        "capture_sha256": tape.sha256,
        "capture_start_utc": utc_string(start),
        "capture_end_utc": utc_string(end),
        "capture_seconds": Decimal(end - start) / SECOND_US,
        "data_quality": tape.quality,
        "full_window": evaluate(tape.frames, settings, ledger=ledger),
        "latency_sensitivity_ms": {
            delay: evaluate(tape.frames, settings.model_copy(update={"latency_ms": delay}))
            for delay in (100, 500)
            if delay != settings.latency_ms
        },
    }


def _research_metadata(settings: ReplaySettings) -> dict:
    return {
        "strategy_version": VERSION,
        "venue": "delta_india",
        "symbol": "BTCUSD",
        "settings": settings.model_dump(mode="json"),
    }


def research(tape: Tape, settings: ReplaySettings, *, ledger: bool = False) -> dict:
    frames = tape.frames
    start, end = frames[0].time_us, frames[-1].time_us
    report = _research_metadata(settings) | _capture_report(tape, settings, ledger=ledger)
    split = start + (end - start) * 4 // 5
    index = bisect_left([frame.time_us for frame in frames], split)
    if 0 < index < len(frames):
        report["chronological_windows"] = {
            "split_utc": utc_string(split),
            "earlier": evaluate(frames[:index], settings),
            "later": evaluate(frames[index:], settings),
        }
    return report


def _combine_windows(windows: list[dict]) -> dict:
    combined = {}
    for name in STRATEGIES:
        portfolios = [window[name]["portfolio"] for window in windows]
        closed = sum(row["closed_trades"] for row in portfolios)
        pnl = sum((row["realized_pnl_inr"] for row in portfolios), Decimal(0))
        unresolved = Counter(
            row["censored_position"]["reason"] for row in portfolios if row["censored_position"]
        )
        horizons = {}
        for horizon in HORIZONS_SECONDS:
            samples = [window[name]["markouts"]["horizons_seconds"][horizon] for window in windows]
            observed = sum(row["observed"] for row in samples)
            missing: Counter[str] = Counter()
            for row in samples:
                missing.update(row["missing"])
            weighted_net_bps = sum(
                (row["mean_net_bps"] * row["observed"] for row in samples if row["observed"]),
                Decimal(0),
            )
            horizons[horizon] = {
                "observed": observed,
                "missing": dict(missing),
                "mean_net_bps": weighted_net_bps / observed if observed else None,
                "positive_net": sum(row.get("positive_net", 0) for row in samples),
            }
        combined[name] = {
            "portfolio": {
                "signals": sum(row["signals"] for row in portfolios),
                "closed_trades": closed,
                "realized_pnl_inr": pnl,
                "closed_trade_expectancy_inr": pnl / closed if closed else None,
                "worst_session_mark_drawdown": max(row["mark_drawdown"] for row in portfolios),
                "unresolved_positions": dict(unresolved),
                "unprocessed_signals": sum(row["unprocessed_signals"] for row in portfolios),
            },
            "markouts": {"horizons_seconds": horizons},
        }
    return combined


def _session_summary(sessions: list[dict]) -> dict:
    return {
        "session_count": len(sessions),
        "capture_start_utc": sessions[0]["capture_start_utc"],
        "capture_end_utc": sessions[-1]["capture_end_utc"],
        "capture_seconds": sum(session["capture_seconds"] for session in sessions),
        "full_window": _combine_windows([session["full_window"] for session in sessions]),
    }


def research_sessions(paths: list[Path], settings: ReplaySettings, *, ledger: bool = False) -> dict:
    if len(paths) < 2:
        raise ValueError("Multi-session research requires at least two capture files.")
    sessions = [
        {"capture_file": path.name} | _capture_report(load_tape(path), settings, ledger=ledger)
        for path in paths
    ]
    sessions.sort(key=lambda session: session["capture_start_utc"])
    for previous, current in zip(sessions, sessions[1:]):
        if current["capture_start_utc"] <= previous["capture_end_utc"]:
            raise ValueError(
                f"Capture windows overlap: {previous['capture_file']} and "
                f"{current['capture_file']}."
            )
    split = len(sessions) * 4 // 5
    return (
        _research_metadata(settings)
        | _session_summary(sessions)
        | {
            "initial_cash_resets_per_session": True,
            "sessions": sessions,
            "chronological_sessions": {
                "earlier": _session_summary(sessions[:split]),
                "later": _session_summary(sessions[split:]),
            },
            "latency_sensitivity_ms": {
                delay: _combine_windows(
                    [session["latency_sensitivity_ms"][delay] for session in sessions]
                )
                for delay in sessions[0]["latency_sensitivity_ms"]
            },
        }
    )
