import json
from decimal import Decimal
from pathlib import Path
from zlib import crc32

import pytest

from app.backtest.engine import simulate
from app.backtest.fills import net_pnl, sweep
from app.backtest.models import ReplaySettings
from app.market.book_tape import Frame, load_tape
from app.market.orderbook import BookError, OrderBook, Quote
from app.strategies.orderbook_imbalance import Signal, find_signals


def snapshot() -> dict:
    asks, bids = [["101.0", "10"], ["102.0", "10"]], [["100.0", "30"], ["99.0", "30"]]
    return {
        "type": "ob_updates",
        "sy": "BTCUSD",
        "action": "snapshot",
        "seq": 10,
        "a": asks,
        "b": bids,
        "cs": crc32(b"101.0:10,102.0:10|100.0:30,99.0:30"),
    }


def test_depth_replaces_sizes_and_checks_the_resulting_book():
    book = OrderBook()
    assert book.apply(snapshot()).imbalance(2) == Decimal(".5")
    update = {
        "action": "update",
        "seq": 11,
        "a": [["101.0", "0"]],
        "b": [["100.0", "15"]],
        "cs": crc32(b"102.0:10|100.0:15,99.0:30"),
    }
    quote = book.apply(update)
    assert quote.ask == 102 and quote.bids[0] == (Decimal(100), 15)
    with pytest.raises(BookError, match="checksum"):
        book.apply(update | {"seq": 12, "cs": 0})


def test_gap_requires_a_new_snapshot_and_taker_side_uses_buyer_role(tmp_path: Path):
    tape_path = tmp_path / "book.jsonl"
    messages = [
        snapshot(),
        {"type": "trades", "sy": "BTCUSD", "r": "t", "s": 20.0},
        snapshot() | {"action": "update", "seq": 12},
        {"type": "trades", "sy": "BTCUSD", "r": "m", "s": 10.0},
        snapshot() | {"seq": 40},
    ]
    rows = [{"schema": 1, "venue": "delta_india", "symbol": "BTCUSD"}]
    rows.extend({"arrival_us": i, "connection": 1, "message": m} for i, m in enumerate(messages))
    tape_path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")

    tape = load_tape(tape_path)
    assert tape.frames[1].signed_contracts == 20
    assert tape.frames[2].quote is None and tape.frames[3].quote is None
    assert tape.frames[3].signed_contracts == -10
    assert tape.frames[4].segment != tape.frames[1].segment
    assert tape.quality["book_snapshots"] == 2


def frame(time_us: int, bid: str = "60000", signed: int = 0) -> Frame:
    price = Decimal(bid)
    quote = Quote(
        ((price, 2000), (price - 1, 2000)),
        ((price + Decimal(".5"), 500), (price + 1, 500)),
    )
    return Frame(time_us, 1, quote, time_us, price, time_us, signed)


def test_confirmed_entry_waits_for_taker_buying():
    frames = [
        frame(0),
        frame(1_000_000, signed=-50),
        frame(2_000_000),
        frame(2_100_000, signed=100),
    ]
    base, confirmed = find_signals(frames, ReplaySettings(levels=2))
    assert [signal.index for signal in base] == [2]
    assert [signal.index for signal in confirmed] == [3]


def test_replay_delays_entry_and_stop_and_charges_both_fees():
    frames = [
        frame(0),
        frame(100_000, "60010"),
        frame(300_000, "60020"),
        frame(500_000, "59800"),
        frame(800_000, "59775"),
    ]
    settings = ReplaySettings(levels=2)
    signal = Signal(0, Decimal(".6"), 50, 50, Decimal(".1"))
    summary, trades = simulate(frames, [f.time_us for f in frames], [signal], settings)
    [trade] = trades
    assert trade.entry_us == 300_000
    assert trade.exit_us == 800_000 and trade.reason == "STOP_LOSS"
    assert trade.entry.average_usd == Decimal("60020.5")
    assert trade.bracket.stop_usd == Decimal("59820.5")
    assert trade.exit is not None and trade.exit.average_usd == 59775
    assert trade.pnl_inr == net_pnl(trade.entry, trade.exit, settings.fee_rate)
    assert trade.pnl_inr < -trade.bracket.planned_loss_inr
    assert summary["realized_pnl_inr"] == trade.pnl_inr


def test_executions_walk_depth_and_open_position_is_censored_at_end():
    bids = ((Decimal(100), 1), (Decimal(99), 2))
    fill = sweep(bids, 3)
    assert fill is not None and fill.average_usd == Decimal(298) / 3
    assert sweep(bids, 4) is None

    frames = [frame(0), frame(300_000)]
    signal = Signal(0, Decimal(".6"), 50, 50, Decimal(".1"))
    summary, trades = simulate(frames, [f.time_us for f in frames], [signal], ReplaySettings())
    assert not trades
    assert summary["censored_position"]["reason"] == "capture_end"
    assert summary["unprocessed_signals"] == 0
